from datetime import timedelta
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.models import Notification, utcnow
from app.notifications import deliver_pending
from app.schemas import LeadCreate
from app.services import create_lead
from tests.conftest import PAYLOAD


async def test_notification_success_and_no_duplicate(running_app):
    sessions = running_app.state.sessions
    await create_lead(sessions, LeadCreate(**PAYLOAD))
    bot = AsyncMock()
    assert await deliver_pending(sessions, bot, 42) == 1
    assert bot.send_message.await_count == 1
    assert bot.send_message.call_args.args[0] == 42
    assert await deliver_pending(sessions, bot, 42) == 0
    async with sessions() as session:
        notification = await session.scalar(select(Notification))
        assert notification.sent_at is not None and notification.attempts == 1


async def test_notification_failure_retries_without_losing_lead(running_app, caplog):
    sessions = running_app.state.sessions
    await create_lead(sessions, LeadCreate(**PAYLOAD))
    bot = AsyncMock()
    bot.send_message.side_effect = RuntimeError("secret-personal-data")
    await deliver_pending(sessions, bot, 42)
    assert "secret-personal-data" not in caplog.text
    async with sessions.begin() as session:
        notification = await session.scalar(select(Notification))
        assert notification.sent_at is None and notification.attempts == 1
        notification.next_attempt_at = utcnow() - timedelta(seconds=1)
    bot.send_message.side_effect = None
    await deliver_pending(sessions, bot, 42)
    async with sessions() as session:
        notification = await session.scalar(select(Notification))
        assert notification.sent_at is not None and notification.attempts == 2
