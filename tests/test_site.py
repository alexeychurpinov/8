from __future__ import annotations

import importlib
import sys
import time
from collections import deque

import httpx
import pytest

VALID_KEY = "sensitive-example-key-that-is-long-enough-12345"
ORIGINAL_ASYNC_CLIENT = httpx.AsyncClient
LEAD = {
    "name": "Анна Иванова",
    "phone": "+79991234567",
    "email": "anna@example.com",
    "service": "Консультация",
    "comment": "После 18:00",
    "source": "website-form",
}


@pytest.fixture
def site_module(monkeypatch):
    monkeypatch.setenv("API_KEY", VALID_KEY)
    monkeypatch.setenv("LEADS_API_URL", "http://127.0.0.1:8000/api/leads")
    if "examples.site" in sys.modules:
        return importlib.reload(sys.modules["examples.site"])
    return importlib.import_module("examples.site")


async def post_via_site(monkeypatch, site, handler, payload=None, *, content=None, headers=None):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        site.httpx,
        "AsyncClient",
        lambda **kwargs: ORIGINAL_ASYNC_CLIENT(transport=transport, **kwargs),
    )
    async with ORIGINAL_ASYNC_CLIENT(
        transport=httpx.ASGITransport(app=site.app), base_url="http://test"
    ) as client:
        return await client.post(
            "/submit",
            json=payload,
            content=content,
            headers=headers,
        )


@pytest.mark.asyncio
async def test_proxy_forwards_lead_and_keeps_key_out_of_html(site_module, monkeypatch):
    def backend(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "http://127.0.0.1:8000/api/leads"
        assert request.headers["X-API-Key"] == VALID_KEY
        assert request.read() == httpx.Request(
            "POST", "http://example.test", json=LEAD
        ).read()
        return httpx.Response(
            201,
            json={
                "id": 17,
                "status": "new",
                "created_at": "2026-01-01T00:00:00Z",
                "notification_status": "queued",
            },
        )

    response = await post_via_site(monkeypatch, site_module, backend, LEAD)
    assert response.status_code == 201
    assert response.json()["id"] == 17

    async with ORIGINAL_ASYNC_CLIENT(
        transport=httpx.ASGITransport(app=site_module.app), base_url="http://test"
    ) as client:
        page = await client.get("/")
    assert page.status_code == 200
    assert VALID_KEY not in page.text
    assert "X-API-Key" not in page.text


@pytest.mark.asyncio
async def test_backend_validation_error_is_sanitized(site_module, monkeypatch):
    def backend(_: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": [{"input": "private-value"}]})

    response = await post_via_site(monkeypatch, site_module, backend, LEAD)
    assert response.status_code == 422
    assert "private-value" not in response.text


@pytest.mark.asyncio
async def test_body_limit_rejects_before_backend(site_module, monkeypatch):
    def unexpected_backend(_: httpx.Request) -> httpx.Response:
        raise AssertionError("oversized request must not reach backend")

    response = await post_via_site(
        monkeypatch,
        site_module,
        unexpected_backend,
        content=b"x" * (site_module.MAX_BODY_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_rate_limit_is_applied_before_backend(site_module, monkeypatch):
    site_module._requests_by_ip["127.0.0.1"] = deque(
        [time.monotonic()] * site_module.RATE_LIMIT_PER_MINUTE
    )

    def unexpected_backend(_: httpx.Request) -> httpx.Response:
        raise AssertionError("rate-limited request must not reach backend")

    response = await post_via_site(monkeypatch, site_module, unexpected_backend, LEAD)
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
