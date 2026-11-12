from datetime import UTC, datetime
from html import unescape
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Chat, ErrorEvent, Message, Update, User

from app.bot import (
    _detail_keyboard,
    _parse_callback,
    _render_full_comment,
    build_dispatcher,
    lead_keyboard,
    render_lead,
)


def make_lead(**overrides):
    values = {
        "id": 17,
        "name": "<script>alert('x')</script>",
        "phone": "+7 (900) 123-45-67",
        "email": "client@example.test",
        "service": "Consultation & setup",
        "comment": "<img src=x>",
        "source": "website",
        "status": "new",
        "created_at": datetime(2026, 1, 2, tzinfo=UTC),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_render_lead_escapes_html_and_includes_details():
    output = render_lead(make_lead(), detailed=True)

    assert "&lt;script&gt;" in output
    assert "&lt;img src=x&gt;" in output
    assert "<script>" not in output
    assert "<b>Email:</b> client@example.test" in output
    assert len(output) < 4096


def test_detailed_card_keeps_full_email_and_source():
    email = "x" * 241 + "@example.test"
    source = "Источник " + "&" * 191
    output = render_lead(
        make_lead(email=email, source=source, name="И" * 120, service="У" * 160),
        detailed=True,
    )

    assert email in unescape(output)
    assert source in unescape(output)
    assert "И" * 120 in unescape(output)
    assert "У" * 160 in unescape(output)
    assert len(output) < 4096


def test_full_comment_is_available_in_a_separate_telegram_message():
    comment = "&" * 1500
    output = _render_full_comment(make_lead(comment=comment))
    visible_text = unescape(output)

    assert comment in visible_text
    assert "&amp;" in output
    assert len(visible_text.encode("utf-16-le")) // 2 < 4096


def test_comment_callback_is_present_on_lead_details_keyboard():
    keyboard = _detail_keyboard(make_lead(comment="full comment"), "all", 0)
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert "comment:17:all:0" in callbacks
    assert _parse_callback("comment:17:all:0") == ("comment", "17", "all", "0")


def test_lead_keyboard_has_safe_status_actions():
    keyboard = lead_keyboard(make_lead())
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert "action:17:accepted:all:0" in callbacks
    assert "action:17:rejected:all:0" in callbacks
    assert "detail:17:all:0" in callbacks
    assert all(_parse_callback(value) for value in callbacks)


@pytest.mark.parametrize(
    "value",
    ["filter:invalid", "action:1:accepted:all:0:extra", "detail:-1:all:0", None],
)
def test_callback_data_rejects_unexpected_values(value):
    assert _parse_callback(value) is None


def make_message(user_id: int, chat_type: ChatType = ChatType.PRIVATE) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=100, type=chat_type),
        from_user=User(id=user_id, is_bot=False, first_name="Admin"),
        text="/start",
    )


def make_callback(
    user_id: int,
    chat_type: ChatType = ChatType.PRIVATE,
    data: str = "filter:all",
) -> CallbackQuery:
    return CallbackQuery(
        id="callback-id",
        from_user=User(id=user_id, is_bot=False, first_name="Admin"),
        chat_instance="instance",
        data=data,
        message=make_message(user_id, chat_type),
    )


@pytest.mark.asyncio
async def test_message_middleware_allows_only_admin_in_private_chat():
    dispatcher = build_dispatcher(sessions=None, admin_id=123)
    middleware = dispatcher.sub_routers[0].message.outer_middleware._middlewares[0]
    handler = AsyncMock(return_value="handled")

    result = await middleware(handler, make_message(123), {})
    assert result == "handled"
    handler.assert_awaited_once()

    handler.reset_mock()
    assert await middleware(handler, make_message(456), {}) is None
    assert await middleware(handler, make_message(123, ChatType.GROUP), {}) is None
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_middleware_denies_non_admin():
    dispatcher = build_dispatcher(sessions=None, admin_id=123)
    middleware = dispatcher.sub_routers[0].callback_query.outer_middleware._middlewares[0]
    handler = AsyncMock(return_value="handled")
    callbacks = (make_callback(456), make_callback(123, ChatType.GROUP))

    with patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as answer:
        for callback in callbacks:
            assert await middleware(handler, callback, {}) is None

    assert answer.await_count == 2
    answer.assert_awaited_with("Доступ запрещён", show_alert=True)
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_middleware_allows_admin_in_private_chat():
    dispatcher = build_dispatcher(sessions=None, admin_id=123)
    middleware = dispatcher.sub_routers[0].callback_query.outer_middleware._middlewares[0]
    handler = AsyncMock(return_value="handled")

    result = await middleware(handler, make_callback(123), {})

    assert result == "handled"
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_router_error_log_contains_only_exception_type(caplog):
    dispatcher = build_dispatcher(sessions=None, admin_id=123)
    router = dispatcher.sub_routers[0]
    error_handler = router.errors.handlers[0].callback

    await error_handler(
        ErrorEvent(
            update=Update(update_id=1),
            exception=RuntimeError("private customer data and bot token"),
        )
    )

    assert "RuntimeError" in caplog.text
    assert "private customer data" not in caplog.text
    assert "bot token" not in caplog.text


@pytest.mark.asyncio
async def test_full_comment_callback_sends_escaped_complete_comment():
    dispatcher = build_dispatcher(sessions=None, admin_id=123)
    handler = dispatcher.sub_routers[0].callback_query.handlers[0].callback
    callback = make_callback(123, data="comment:17:all:0")
    comment = "&" * 1500

    with (
        patch("app.bot.services.get_lead", new=AsyncMock(return_value=make_lead(comment=comment))),
        patch.object(Message, "answer", new_callable=AsyncMock) as answer,
        patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as acknowledge,
    ):
        await handler(callback)

    sent_text = answer.await_args.args[0]
    assert comment in unescape(sent_text)
    assert "&amp;" in sent_text
    assert len(unescape(sent_text).encode("utf-16-le")) // 2 < 4096
    acknowledge.assert_awaited_once_with()
