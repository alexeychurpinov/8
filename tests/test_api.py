import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.main import create_app
from app.models import Lead, LeadHistory, Notification
from app.services import get_lead
from tests.conftest import HEADERS, PAYLOAD


async def test_create_persists_atomically(client, running_app):
    response = await client.post("/api/leads", json=PAYLOAD, headers=HEADERS)
    assert response.status_code == 201
    assert response.json()["notification_status"] == "queued"
    sessions = running_app.state.sessions
    lead = await get_lead(sessions, response.json()["id"])
    assert lead.phone == "+79991234567"
    assert lead.status == "new"
    async with sessions() as session:
        history = await session.scalar(select(LeadHistory))
        assert history.to_status == "new" and history.from_status is None
        notification = await session.scalar(select(Notification))
        assert notification.lead_id == lead.id and notification.sent_at is None
    assert "phone" not in response.json()


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}])
async def test_auth_required(client, running_app, headers):
    response = await client.post("/api/leads", json=PAYLOAD, headers=headers)
    assert response.status_code == 401
    async with running_app.state.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Lead)) == 0


@pytest.mark.parametrize("field,value", [
    ("name", "   "), ("phone", "123"), ("phone", "letters1234567"),
    ("email", "invalid"), ("service", ""), ("source", ""),
    ("comment", "x" * 1501), ("name", "x" * 121), ("comment", "\x00"),
    ("phone", "+1234567890123456"), ("unknown", "value"),
])
async def test_validation(client, field, value):
    response = await client.post("/api/leads", json={**PAYLOAD, field: value}, headers=HEADERS)
    assert response.status_code == 422
    assert all("input" not in error for error in response.json()["detail"])


async def test_limits_and_malformed_json(client):
    response = await client.post(
        "/api/leads", content="x" * 17000,
        headers={**HEADERS, "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    response = await client.post("/api/leads", content="invalid", headers=HEADERS)
    assert response.status_code == 415
    response = await client.post(
        "/api/leads", content="{", headers={**HEADERS, "Content-Type": "application/json"}
    )
    assert response.status_code == 422

    async def stream():
        yield b"x" * 9000
        yield b"x" * 9000

    response = await client.post(
        "/api/leads", content=stream(), headers={**HEADERS, "Content-Type": "application/json"}
    )
    assert response.status_code == 413


async def test_rate_limit_persists_after_restart(settings):
    settings.rate_limit_per_minute = 1
    for expected in [201, 429]:
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post("/api/leads", json=PAYLOAD, headers=HEADERS)
                assert response.status_code == expected
                if expected == 429:
                    assert 1 <= int(response.headers["Retry-After"]) <= 60


async def test_failed_auth_is_rate_limited(settings):
    settings.auth_rate_limit_per_minute = 2
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            responses = [await client.post("/api/leads", json=PAYLOAD) for _ in range(3)]
            assert [response.status_code for response in responses] == [401, 401, 429]


async def test_concurrent_submission_limit(settings):
    settings.rate_limit_per_minute = 3
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            results = await asyncio.gather(*[
                client.post("/api/leads", json=PAYLOAD, headers=HEADERS) for _ in range(8)
            ])
            assert sum(response.status_code == 201 for response in results) == 3
            assert sum(response.status_code == 429 for response in results) == 5


async def test_health(client):
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "telegram_enabled": False}


async def test_storage_failure_is_sanitized(client, monkeypatch):
    async def fail(*args):
        raise RuntimeError("private name, SQL parameters and secret")

    monkeypatch.setattr("app.main.create_lead", fail)
    response = await client.post("/api/leads", json=PAYLOAD, headers=HEADERS)
    assert response.status_code == 503
    assert "private" not in response.text
