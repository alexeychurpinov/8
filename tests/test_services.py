import asyncio

import pytest
from sqlalchemy import func, select

from app.database import create_database, init_database
from app.models import LeadHistory, Notification
from app.schemas import LeadCreate
from app.services import (
    change_status,
    create_lead,
    get_history,
    get_lead,
    get_stats,
    list_leads,
)
from tests.conftest import PAYLOAD


async def test_lifecycle_history_and_filters(running_app):
    sessions = running_app.state.sessions
    lead = await create_lead(sessions, LeadCreate(**PAYLOAD))
    other = await create_lead(sessions, LeadCreate(**PAYLOAD))
    accepted = await change_status(sessions, lead.id, "accepted", 42)
    assert accepted.status == "accepted"
    processed = await change_status(sessions, lead.id, "processed", 42)
    assert processed.status == "processed"
    await change_status(sessions, other.id, "rejected", 42)
    with pytest.raises(ValueError):
        await change_status(sessions, lead.id, "rejected", 42)
    with pytest.raises(ValueError):
        await change_status(sessions, 999, "accepted", 42)
    with pytest.raises(ValueError):
        await change_status(sessions, lead.id, "unknown", 42)
    history = await get_history(sessions, lead.id)
    assert [item.to_status for item in history] == ["processed", "accepted", "new"]
    assert history[0].actor_telegram_id == 42
    assert await get_stats(sessions) == {
        "new": 0, "accepted": 0, "processed": 1, "rejected": 1, "total": 2
    }
    filtered, total = await list_leads(sessions, status="processed")
    assert total == 1 and filtered[0].id == lead.id
    page, total = await list_leads(sessions, page=1, page_size=1)
    assert len(page) == 1 and total == 2


async def test_concurrent_status_actions_keep_history_consistent(running_app):
    sessions = running_app.state.sessions
    lead = await create_lead(sessions, LeadCreate(**PAYLOAD))
    results = await asyncio.gather(
        change_status(sessions, lead.id, "accepted", 42),
        change_status(sessions, lead.id, "accepted", 42),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, ValueError) for result in results) == 1
    history = await get_history(sessions, lead.id)
    assert len(history) == 2


async def test_new_cannot_be_processed(running_app):
    sessions = running_app.state.sessions
    lead = await create_lead(sessions, LeadCreate(**PAYLOAD))
    with pytest.raises(ValueError):
        await change_status(sessions, lead.id, "processed", 42)
    assert (await get_lead(sessions, lead.id)).status == "new"


async def test_data_survives_reopen(settings):
    engine, sessions = create_database(settings.database_url)
    await init_database(engine)
    lead = await create_lead(sessions, LeadCreate(**PAYLOAD))
    await engine.dispose()
    engine, sessions = create_database(settings.database_url)
    try:
        assert (await get_lead(sessions, lead.id)).name == PAYLOAD["name"]
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(LeadHistory)) == 1
            assert await session.scalar(select(func.count()).select_from(Notification)) == 1
    finally:
        await engine.dispose()


async def test_database_files_are_private(tmp_path):
    import stat

    path = tmp_path / "private" / "leads.db"
    engine, sessions = create_database(f"sqlite+aiosqlite:///{path}")
    try:
        await init_database(engine)
        await create_lead(sessions, LeadCreate(**PAYLOAD))
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        for suffix in ("-wal", "-shm"):
            sidecar = path.with_name(path.name + suffix)
            if sidecar.exists():
                assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
    finally:
        await engine.dispose()
