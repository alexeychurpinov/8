from __future__ import annotations

import asyncio
import ipaddress
import json
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MAX_BODY_BYTES = 16 * 1024
RATE_LIMIT_PER_MINUTE = 20
RATE_WINDOW_SECONDS = 60


class SiteSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
    )

    api_key: SecretStr
    leads_api_url: str = "http://127.0.0.1:8000/api/leads"

    @field_validator("api_key")
    @classmethod
    def valid_api_key(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if len(secret) < 32 or secret.startswith("replace-"):
            raise ValueError("API_KEY must be a random secret of at least 32 characters")
        return value

    @field_validator("leads_api_url")
    @classmethod
    def valid_leads_api_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path != "/api/leads"
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("LEADS_API_URL must be an /api/leads URL without credentials or query")
        if parsed.scheme == "http":
            try:
                is_loopback = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                is_loopback = parsed.hostname == "localhost"
            if not is_loopback:
                raise ValueError("Use HTTPS for non-local LEADS_API_URL endpoints")
        return value


settings = SiteSettings()
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_requests_by_ip: dict[str, deque[float]] = {}
_rate_lock = asyncio.Lock()


async def _rate_limit_exceeded(client_ip: str) -> bool:
    now = time.monotonic()
    async with _rate_lock:
        requests = _requests_by_ip.setdefault(client_ip, deque())
        while requests and now - requests[0] >= RATE_WINDOW_SECONDS:
            requests.popleft()
        if len(requests) >= RATE_LIMIT_PER_MINUTE:
            return True
        requests.append(now)
        if len(_requests_by_ip) > 10_000:
            expired = [
                key
                for key, values in _requests_by_ip.items()
                if not values or now - values[-1] >= RATE_WINDOW_SECONDS
            ]
            for key in expired:
                _requests_by_ip.pop(key, None)
        return False


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(Path(__file__).with_name("index.html"), media_type="text/html")


@app.post("/submit", include_in_schema=False)
async def submit(request: Request) -> JSONResponse:
    client_ip = request.client.host if request.client else "unknown"
    if await _rate_limit_exceeded(client_ip):
        return JSONResponse(
            {"error": "Слишком много отправок. Попробуйте позже."},
            status_code=429,
            headers={"Retry-After": str(RATE_WINDOW_SECONDS)},
        )

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_BODY_BYTES:
                return JSONResponse({"error": "Заявка слишком большая."}, status_code=413)
        except ValueError:
            return JSONResponse({"error": "Некорректный запрос."}, status_code=400)

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            return JSONResponse({"error": "Заявка слишком большая."}, status_code=413)

    if "application/json" not in request.headers.get("content-type", "").lower():
        return JSONResponse({"error": "Ожидается JSON-заявка."}, status_code=415)
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({"error": "Некорректная заявка."}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "Некорректная заявка."}, status_code=400)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                settings.leads_api_url,
                content=bytes(body),
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": settings.api_key.get_secret_value(),
                },
            )
    except httpx.HTTPError:
        return JSONResponse({"error": "Сервис заявок временно недоступен."}, status_code=502)

    if response.status_code == 201:
        try:
            return JSONResponse(response.json(), status_code=201)
        except ValueError:
            return JSONResponse({"error": "Сервис вернул некорректный ответ."}, status_code=502)
    if response.status_code == 422:
        return JSONResponse({"error": "Проверьте заполнение полей формы."}, status_code=422)
    if response.status_code == 413:
        return JSONResponse({"error": "Заявка слишком большая."}, status_code=413)
    if response.status_code == 429:
        return JSONResponse(
            {"error": "Слишком много заявок. Попробуйте позже."},
            status_code=429,
            headers={"Retry-After": response.headers.get("Retry-After", "60")},
        )
    return JSONResponse({"error": "Не удалось отправить заявку."}, status_code=502)
