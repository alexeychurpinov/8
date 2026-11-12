"""Administrative Telegram bot for reviewing incoming leads."""

from __future__ import annotations

import logging
import re
from html import escape
from typing import Any

from aiogram import Dispatcher, Router
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app import services

logger = logging.getLogger(__name__)

STATUSES = ("new", "accepted", "rejected", "processed")
STATUS_LABELS = {
    "new": "Новая",
    "accepted": "Принята",
    "rejected": "Отклонена",
    "processed": "Обработана",
}
FILTER_LABELS = {"all": "Все", **STATUS_LABELS}
PAGE_SIZE = 5
_CALLBACK_RE = re.compile(
    r"(?:(?:filter:(?:all|new|accepted|rejected|processed))|"
    r"(?:page:(?:all|new|accepted|rejected|processed):\d{1,8})|"
    r"(?:detail:\d{1,12}:(?:all|new|accepted|rejected|processed):\d{1,8})|"
    r"(?:comment:\d{1,12}:(?:all|new|accepted|rejected|processed):\d{1,8})|"
    r"(?:action:\d{1,12}:(?:accepted|rejected|processed):"
    r"(?:all|new|accepted|rejected|processed):\d{1,8}))"
)


def _value(value: Any, limit: int | None = None) -> str:
    raw = str(value if value not in (None, "") else "—")
    if limit is not None and len(raw) > limit:
        raw = raw[: limit - 1] + "…"
    return escape(raw)


def render_lead(lead: Any, detailed: bool = False) -> str:
    """Render an escaped Telegram HTML card from a lead model or compatible object."""
    status = getattr(lead, "status", "new")
    status_text = STATUS_LABELS.get(status, "Неизвестен")
    lines = [
        f"<b>Заявка №{_value(getattr(lead, 'id', '—'), 12)}</b>",
        f"<b>Статус:</b> {_value(status_text)}",
        f"<b>Имя:</b> {_value(getattr(lead, 'name', None), 120)}",
        f"<b>Телефон:</b> {_value(getattr(lead, 'phone', None), 32)}",
        f"<b>Услуга:</b> {_value(getattr(lead, 'service', None), 160)}",
    ]
    if detailed:
        lines.extend(
            (
                f"<b>Email:</b> {_value(getattr(lead, 'email', None), 254)}",
                f"<b>Источник:</b> {_value(getattr(lead, 'source', None), 200)}",
            )
        )
        comment = getattr(lead, "comment", None) or ""
        if comment:
            preview = comment[:160]
            if len(comment) > len(preview):
                preview += "…"
            lines.append(f"<b>Комментарий:</b> {_value(preview)}")
        else:
            lines.append("<b>Комментарий:</b> —")
    created_at = getattr(lead, "created_at", None)
    if created_at is not None:
        lines.append(f"<b>Создана:</b> {_value(created_at)}")
    return "\n".join(lines)


def _render_full_comment(lead: Any) -> str:
    comment = str(getattr(lead, "comment", None) or "")
    comment = comment[:1500]
    return f"<b>Комментарий к заявке №{_value(lead.id, 12)}</b>\n{escape(comment) or '—'}"


def lead_keyboard(lead: Any) -> InlineKeyboardMarkup:
    """Build notification controls, hiding actions that do not fit the status."""
    lead_id = int(lead.id)
    status = getattr(lead, "status", "new")
    builder = InlineKeyboardBuilder()
    if status == "new":
        builder.row(
            InlineKeyboardButton(
                text="✅ Принять",
                callback_data=f"action:{lead_id}:accepted:all:0",
            ),
            InlineKeyboardButton(
                text="❌ Отклонить",
                callback_data=f"action:{lead_id}:rejected:all:0",
            ),
        )
    elif status == "accepted":
        builder.row(
            InlineKeyboardButton(
                text="✅ Обработана",
                callback_data=f"action:{lead_id}:processed:all:0",
            ),
            InlineKeyboardButton(
                text="❌ Отклонить",
                callback_data=f"action:{lead_id}:rejected:all:0",
            ),
        )
    builder.row(
        InlineKeyboardButton(
            text="Подробнее",
            callback_data=f"detail:{lead_id}:all:0",
        )
    )
    return builder.as_markup()


def _filters_keyboard(selected: str) -> list[list[InlineKeyboardButton]]:
    return [
        [
            InlineKeyboardButton(
                text=("• " if status == selected else "") + label,
                callback_data=f"filter:{status}",
            )
            for status, label in FILTER_LABELS.items()
        ]
    ]


def _lead_list_keyboard(
    leads: list[Any], status: str, page: int, total: int
) -> InlineKeyboardMarkup:
    rows = _filters_keyboard(status)
    for lead in leads:
        name = str(lead.name).replace("\n", " ").replace("\r", " ")
        label = f"№{lead.id} · {name} · {STATUS_LABELS.get(lead.status, lead.status)}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label[:64],
                    callback_data=f"detail:{lead.id}:{status}:{page}",
                )
            ]
        )
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                text="← Назад", callback_data=f"page:{status}:{page - 1}"
            )
        )
    if (page + 1) * PAGE_SIZE < total:
        navigation.append(
            InlineKeyboardButton(
                text="Далее →", callback_data=f"page:{status}:{page + 1}"
            )
        )
    if navigation:
        rows.append(navigation)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _detail_keyboard(lead: Any, status: str, page: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    lead_id = int(lead.id)
    if getattr(lead, "comment", None):
        rows.append(
            [
                InlineKeyboardButton(
                    text="Показать полный комментарий",
                    callback_data=f"comment:{lead_id}:{status}:{page}",
                )
            ]
        )
    if lead.status == "new":
        rows.append(
            [
                InlineKeyboardButton(
                    text="✅ Принять",
                    callback_data=f"action:{lead_id}:accepted:{status}:{page}",
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"action:{lead_id}:rejected:{status}:{page}",
                ),
            ]
        )
    elif lead.status == "accepted":
        rows.append(
            [
                InlineKeyboardButton(
                    text="✅ Обработана",
                    callback_data=f"action:{lead_id}:processed:{status}:{page}",
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"action:{lead_id}:rejected:{status}:{page}",
                ),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="← К списку", callback_data=f"page:{status}:{page}"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _status_filter(raw: str) -> str | None:
    return None if raw == "all" else raw


def _parse_callback(data: str | None) -> tuple[str, ...] | None:
    if not data or not _CALLBACK_RE.fullmatch(data):
        return None
    return tuple(data.split(":"))


async def _list_view(
    sessions: Any, status: str, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    leads, total = await services.list_leads(
        sessions, status=_status_filter(status), page=page, page_size=PAGE_SIZE
    )
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if page >= total_pages:
        page = total_pages - 1
        leads, total = await services.list_leads(
            sessions, status=_status_filter(status), page=page, page_size=PAGE_SIZE
        )
    title = f"<b>Заявки · {FILTER_LABELS[status]}</b>\nВсего: {total} · Страница {page + 1}/{total_pages}"
    if leads:
        summaries = [
            f"<b>Заявка №{_value(item.id)}</b> · {_value(item.name, 20)} · "
            f"{_value(STATUS_LABELS.get(item.status, item.status))}\n"
            f"Услуга: {_value(item.service, 20)}"
            for item in leads
        ]
        text = title + "\n\n" + "\n\n".join(summaries)
    else:
        text = title + "\n\nЗаявок нет."
    return text, _lead_list_keyboard(leads, status, page, total)


async def _detail_view(sessions: Any, lead_id: int) -> str | None:
    lead = await services.get_lead(sessions, lead_id)
    if lead is None:
        return None
    history = await services.get_history(sessions, lead_id, limit=10)
    lines = [render_lead(lead, detailed=True), "", "<b>История изменений</b>"]
    if history:
        for entry in history:
            before = STATUS_LABELS.get(entry.from_status, "—") if entry.from_status else "—"
            after = STATUS_LABELS.get(entry.to_status, entry.to_status)
            actor = f" · администратор {entry.actor_telegram_id}" if entry.actor_telegram_id else ""
            lines.append(
                f"{_value(entry.created_at)}: {_value(before)} → {_value(after)}"
                f"{_value(actor)}"
            )
    else:
        lines.append("Изменений пока нет.")
    return "\n".join(lines)


def build_dispatcher(sessions: Any, admin_id: int) -> Dispatcher:
    """Create the bot dispatcher; polling and Bot lifecycle belong to the caller."""
    router = Router(name="lead-admin")

    class AdminOnlyMiddleware(BaseMiddleware):
        async def __call__(self, handler: Any, event: Any, data: dict[str, Any]) -> Any:
            user = getattr(event, "from_user", None)
            chat = getattr(event, "chat", None)
            if chat is None:
                message = getattr(event, "message", None)
                chat = getattr(message, "chat", None)
            if user is None or user.id != admin_id or chat is None or chat.type != ChatType.PRIVATE:
                if isinstance(event, CallbackQuery):
                    await event.answer("Доступ запрещён", show_alert=True)
                return None
            return await handler(event, data)

    router.message.outer_middleware(AdminOnlyMiddleware())
    router.callback_query.outer_middleware(AdminOnlyMiddleware())

    @router.errors()
    async def log_unhandled_error(event: ErrorEvent) -> None:
        logger.error("Unhandled Telegram update exception type: %s", type(event.exception).__name__)

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await message.answer(
            "Здравствуйте! Здесь можно управлять заявками.\n"
            "Команды: /leads — список заявок, /stats — статистика."
        )

    @router.message(Command("leads"))
    async def leads_command(message: Message) -> None:
        text_args = (message.text or "").split(maxsplit=1)
        requested = text_args[1].strip().lower() if len(text_args) > 1 else "all"
        status = requested if requested in FILTER_LABELS else "all"
        try:
            text, keyboard = await _list_view(sessions, status, 0)
            await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        except Exception as exc:
            logger.error("Failed to list leads; exception type: %s", type(exc).__name__)
            await message.answer("Не удалось загрузить заявки. Попробуйте позже.")

    @router.message(Command("stats"))
    async def stats_command(message: Message) -> None:
        try:
            stats = await services.get_stats(sessions)
            lines = ["<b>Статистика заявок</b>"]
            for status in STATUSES:
                lines.append(f"{STATUS_LABELS[status]}: {_value(stats.get(status, 0))}")
            lines.append(f"Всего: {_value(stats.get('total', 0))}")
            await message.answer("\n".join(lines), parse_mode="HTML")
        except Exception as exc:
            logger.error(
                "Failed to load lead statistics; exception type: %s",
                type(exc).__name__,
            )
            await message.answer("Не удалось загрузить статистику. Попробуйте позже.")

    @router.callback_query()
    async def callbacks(callback: CallbackQuery) -> None:
        parsed = _parse_callback(callback.data)
        if parsed is None:
            await callback.answer("Некорректная команда", show_alert=True)
            return
        message = callback.message
        if message is None:
            await callback.answer()
            return
        try:
            kind = parsed[0]
            if kind == "filter":
                status = parsed[1]
                text, keyboard = await _list_view(sessions, status, 0)
                await message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
                await callback.answer()
            elif kind == "page":
                status, page = parsed[1], int(parsed[2])
                text, keyboard = await _list_view(sessions, status, page)
                await message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
                await callback.answer()
            elif kind == "detail":
                lead_id, status, page = int(parsed[1]), parsed[2], int(parsed[3])
                lead = await services.get_lead(sessions, lead_id)
                if lead is None:
                    await callback.answer("Заявка не найдена", show_alert=True)
                    return
                text = await _detail_view(sessions, lead_id)
                await message.edit_text(
                    text or "Заявка не найдена.",
                    reply_markup=_detail_keyboard(lead, status, page),
                    parse_mode="HTML",
                )
                await callback.answer()
            elif kind == "comment":
                lead_id = int(parsed[1])
                lead = await services.get_lead(sessions, lead_id)
                if lead is None:
                    await callback.answer("Заявка не найдена", show_alert=True)
                    return
                await message.answer(_render_full_comment(lead), parse_mode="HTML")
                await callback.answer()
            else:
                lead_id, target, status, page = (
                    int(parsed[1]), parsed[2], parsed[3], int(parsed[4])
                )
                try:
                    lead = await services.change_status(
                        sessions, lead_id, target, callback.from_user.id
                    )
                except ValueError:
                    await callback.answer("Изменение статуса невозможно", show_alert=True)
                    return
                await message.edit_text(
                    render_lead(lead),
                    reply_markup=lead_keyboard(lead),
                    parse_mode="HTML",
                )
                await callback.answer("Статус обновлён")
        except Exception as exc:
            logger.error(
                "Failed to handle lead bot callback; exception type: %s",
                type(exc).__name__,
            )
            await callback.answer("Не удалось выполнить действие. Попробуйте позже.", show_alert=True)

    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher
