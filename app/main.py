import asyncio
import logging
from contextlib import asynccontextmanager

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from sqlalchemy import text

from app.bot import build_dispatcher
from app.config import Settings
from app.database import create_database, init_database
from app.notifications import notification_worker
from app.schemas import LeadCreate, LeadReceipt
from app.security import LeadSecurityMiddleware
from app.services import create_lead

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
        # Third-party request/error logs can include URLs with the bot token.
        for name in ("aiogram", "aiohttp", "httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.CRITICAL)
        config = settings or Settings()
        engine, sessions = create_database(config.database_url)
        app.state.settings = config
        app.state.sessions = sessions
        app.state.background_tasks = []
        app.state.telegram_ready = not config.telegram_enabled
        bot = None
        try:
            await init_database(engine)
            if config.telegram_enabled:
                bot = Bot(
                    config.telegram_bot_token.get_secret_value(),
                    default=DefaultBotProperties(parse_mode="HTML"),
                )
                dispatcher = build_dispatcher(sessions, config.admin_telegram_id)

                async def poll():
                    while True:
                        try:
                            await bot.get_me(request_timeout=10)
                            await bot.set_my_commands([
                                BotCommand(command="start", description="Помощь"),
                                BotCommand(command="leads", description="Заявки"),
                                BotCommand(command="stats", description="Статистика"),
                            ], request_timeout=10)
                            app.state.telegram_ready = True
                            await dispatcher.start_polling(
                                bot, handle_signals=False, close_bot_session=False,
                                handle_as_tasks=False,
                                allowed_updates=dispatcher.resolve_used_update_types(),
                            )
                        except Exception as exc:
                            logger.error("Telegram polling error: %s", type(exc).__name__)
                        finally:
                            app.state.telegram_ready = False
                        await asyncio.sleep(5)

                app.state.background_tasks = [
                    asyncio.create_task(poll(), name="telegram-polling"),
                    asyncio.create_task(
                        notification_worker(sessions, bot, config.admin_telegram_id),
                        name="telegram-notifications",
                    ),
                ]
            yield
        finally:
            for task in app.state.background_tasks:
                task.cancel()
            await asyncio.gather(*app.state.background_tasks, return_exceptions=True)
            if bot:
                await bot.session.close()
            await engine.dispose()

    app = FastAPI(
        title="Telegram Lead Manager", version="1.0.0", lifespan=lifespan,
        description="Send leads from your server. Never publish X-API-Key in browser code.",
    )
    app.add_middleware(LeadSecurityMiddleware)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(status_code=422, content={
            "detail": [
                {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
                for error in exc.errors()
            ]
        })

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        logger.error("Request failed: %s", type(exc).__name__)
        return JSONResponse(status_code=503, content={"detail": "Service temporarily unavailable"})

    @app.post(
        "/api/leads", response_model=LeadReceipt, status_code=201,
        dependencies=[Depends(APIKeyHeader(name="X-API-Key", auto_error=False))],
    )
    async def submit_lead(payload: LeadCreate, request: Request):
        lead = await create_lead(request.app.state.sessions, payload)
        return LeadReceipt(id=lead.id, status=lead.status, created_at=lead.created_at)

    @app.get("/healthz")
    async def health(request: Request):
        async with request.app.state.sessions() as session:
            await session.execute(text("SELECT 1"))
        healthy = request.app.state.telegram_ready and all(
            not task.done() for task in request.app.state.background_tasks
        )
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={"status": "ok" if healthy else "degraded",
                     "telegram_enabled": request.app.state.settings.telegram_enabled},
        )

    return app


app = create_app()
