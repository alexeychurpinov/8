from sqlalchemy import func, select, update

from app.models import Lead, LeadHistory, Notification, utcnow
from app.schemas import LeadCreate

STATUSES = ("new", "accepted", "rejected", "processed")
TRANSITIONS = {"new": {"accepted", "rejected"}, "accepted": {"processed", "rejected"}}


async def create_lead(sessions, payload: LeadCreate) -> Lead:
    async with sessions.begin() as session:
        lead = Lead(**payload.model_dump())
        session.add(lead)
        await session.flush()
        session.add_all([
            LeadHistory(lead_id=lead.id, from_status=None, to_status="new"),
            Notification(lead_id=lead.id),
        ])
    return lead


async def get_lead(sessions, lead_id: int) -> Lead | None:
    async with sessions() as session:
        return await session.get(Lead, lead_id)


async def list_leads(sessions, status=None, page=0, page_size=5):
    if status is not None and status not in STATUSES:
        raise ValueError("Unknown status")
    page = max(page, 0)
    page_size = min(max(page_size, 1), 20)
    query = select(Lead)
    count = select(func.count()).select_from(Lead)
    if status:
        query = query.where(Lead.status == status)
        count = count.where(Lead.status == status)
    async with sessions() as session:
        total = await session.scalar(count)
        leads = await session.scalars(
            query.order_by(Lead.id.desc()).offset(page * page_size).limit(page_size)
        )
        return list(leads), total


async def get_history(sessions, lead_id, limit=10):
    async with sessions() as session:
        rows = await session.scalars(
            select(LeadHistory).where(LeadHistory.lead_id == lead_id)
            .order_by(LeadHistory.id.desc()).limit(limit)
        )
        return list(rows)


async def get_stats(sessions):
    async with sessions() as session:
        rows = await session.execute(select(Lead.status, func.count()).group_by(Lead.status))
        result = dict.fromkeys(STATUSES, 0)
        result.update(dict(rows.all()))
        return {**result, "total": sum(result.values())}


async def change_status(sessions, lead_id, target, actor_telegram_id):
    # Conditional UPDATE prevents stale buttons from overwriting another action.
    allowed_from = [state for state, targets in TRANSITIONS.items() if target in targets]
    if not allowed_from:
        raise ValueError("Недопустимый статус")
    async with sessions.begin() as session:
        lead = await session.get(Lead, lead_id)
        if lead is None:
            raise ValueError("Заявка не найдена")
        previous = lead.status
        if previous not in allowed_from:
            raise ValueError("Статус уже изменён или переход запрещён")
        result = await session.execute(
            update(Lead).where(Lead.id == lead_id, Lead.status == previous)
            .values(status=target, updated_at=utcnow())
        )
        if result.rowcount != 1:
            raise ValueError("Статус уже изменён. Откройте заявку заново")
        session.add(LeadHistory(
            lead_id=lead_id, from_status=previous, to_status=target,
            actor_telegram_id=actor_telegram_id,
        ))
        await session.refresh(lead)
    return lead
