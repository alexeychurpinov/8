import asyncio
import logging
from datetime import timedelta

from aiogram.exceptions import TelegramRetryAfter
from sqlalchemy import select

from app.bot import lead_keyboard, render_lead
from app.models import Lead, Notification, utcnow

logger = logging.getLogger(__name__)


async def deliver_pending(sessions, bot, admin_id: int) -> int:
    async with sessions() as session:
        ids = list(await session.scalars(
            select(Notification.id)
            .where(Notification.sent_at.is_(None), Notification.next_attempt_at <= utcnow())
            .order_by(Notification.id).limit(20)
        ))
    for notification_id in ids:
        async with sessions() as session:
            notification = await session.get(Notification, notification_id)
            lead = await session.get(Lead, notification.lead_id)
            attempts = notification.attempts + 1
        delay = min(3600, 2 ** min(attempts, 12))
        sent = False
        try:
            await bot.send_message(
                admin_id, render_lead(lead), reply_markup=lead_keyboard(lead),
                request_timeout=10,
            )
            sent = True
        except TelegramRetryAfter as exc:
            delay = max(delay, exc.retry_after + 1)
            logger.warning("Telegram flood control; notification_id=%s", notification_id)
        except Exception as exc:
            # Exceptions may contain Telegram URLs/tokens or submitted personal data.
            logger.warning(
                "Notification failed; notification_id=%s error_type=%s",
                notification_id, type(exc).__name__,
            )
        async with sessions.begin() as session:
            notification = await session.get(Notification, notification_id)
            notification.attempts = attempts
            if sent:
                notification.sent_at = utcnow()
            else:
                notification.next_attempt_at = utcnow() + timedelta(seconds=delay)
    return len(ids)


async def notification_worker(sessions, bot, admin_id):
    while True:
        try:
            await deliver_pending(sessions, bot, admin_id)
        except Exception as exc:
            logger.error("Notification worker error: %s", type(exc).__name__)
        await asyncio.sleep(2)
