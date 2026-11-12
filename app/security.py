import asyncio
import hashlib
import hmac
import logging
import time

from sqlalchemy import delete
from sqlalchemy.dialects.sqlite import insert
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.models import RateBucket

logger = logging.getLogger(__name__)
MAX_BODY_BYTES = 16 * 1024


async def take_rate_slot(sessions, key: str, limit: int) -> tuple[bool, int]:
    now = int(time.time())
    window = now // 60
    async with sessions.begin() as session:
        await session.execute(delete(RateBucket).where(RateBucket.window < window - 1))
        statement = insert(RateBucket).values(key=key, window=window, count=1)
        count = await session.scalar(statement.on_conflict_do_update(
            index_elements=["key", "window"],
            set_={"count": RateBucket.count + 1},
        ).returning(RateBucket.count))
    return count <= limit, 60 - now % 60


class LeadSecurityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] != "/api/leads":
            return await self.app(scope, receive, send)

        request = Request(scope)
        state = scope["app"].state
        settings = state.settings

        async def reject(status, detail, headers=None):
            await JSONResponse(
                {"detail": detail}, status_code=status, headers=headers
            )(scope, receive, send)

        try:
            address = request.client.host if request.client else "unknown"
            ip_hash = hashlib.sha256(address.encode()).hexdigest()
            allowed, retry = await take_rate_slot(
                state.sessions, "ip:" + ip_hash, settings.auth_rate_limit_per_minute
            )
            if not allowed:
                return await reject(429, "Too many requests", {"Retry-After": str(retry)})
            provided = request.headers.get("X-API-Key", "")
            expected = settings.api_key.get_secret_value()
            if not hmac.compare_digest(provided.encode(), expected.encode()):
                return await reject(401, "Invalid API key")
            allowed, retry = await take_rate_slot(
                state.sessions, "api:" + hashlib.sha256(expected.encode()).hexdigest(),
                settings.rate_limit_per_minute,
            )
            if not allowed:
                return await reject(429, "Too many requests", {"Retry-After": str(retry)})
        except Exception as exc:
            logger.error("API security storage failure: %s", type(exc).__name__)
            return await reject(503, "Service temporarily unavailable")

        size = request.headers.get("content-length")
        if size is not None:
            try:
                if int(size) < 0:
                    raise ValueError
                if int(size) > MAX_BODY_BYTES:
                    return await reject(413, "Request body too large")
            except ValueError:
                return await reject(400, "Invalid Content-Length")
        if scope["method"] == "POST":
            content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type != "application/json":
                return await reject(415, "Content-Type must be application/json")

        body = bytearray()
        try:
            async with asyncio.timeout(10):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > MAX_BODY_BYTES:
                        return await reject(413, "Request body too large")
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            return await reject(408, "Request body timeout")

        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        try:
            await self.app(scope, replay, send)
        except Exception as exc:
            logger.error("Lead request failed: %s", type(exc).__name__)
            await reject(503, "Service temporarily unavailable")
