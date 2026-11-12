import asyncio
from unittest.mock import AsyncMock

from httpx import ASGITransport, AsyncClient

from app.main import create_app


async def test_telegram_tasks_start_and_stop(settings, monkeypatch):
    settings.telegram_enabled = True
    settings.admin_telegram_id = 42
    bot = AsyncMock()
    dispatcher = AsyncMock()
    dispatcher.resolve_used_update_types = lambda: ["message", "callback_query"]
    started = asyncio.Event()

    async def polling(*args, **kwargs):
        assert kwargs["handle_signals"] is False
        assert kwargs["handle_as_tasks"] is False
        started.set()
        await asyncio.Event().wait()

    dispatcher.start_polling.side_effect = polling
    monkeypatch.setattr("app.main.Bot", lambda *args, **kwargs: bot)
    monkeypatch.setattr("app.main.build_dispatcher", lambda *args: dispatcher)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(started.wait(), timeout=2)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/healthz")).status_code == 200
        bot.get_me.assert_awaited_once()
        bot.set_my_commands.assert_awaited_once()
        assert len(app.state.background_tasks) == 2
    assert all(task.done() for task in app.state.background_tasks)
    bot.session.close.assert_awaited_once()
