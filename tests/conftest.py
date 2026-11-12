import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app

API_KEY = "a" * 48
HEADERS = {"X-API-Key": API_KEY}
PAYLOAD = {
    "name": "Анна Иванова",
    "phone": "+7 (999) 123-45-67",
    "email": "anna@example.com",
    "service": "Разработка сайта",
    "comment": "Перезвоните завтра",
    "source": "website",
}


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None, api_key=API_KEY, telegram_enabled=False,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'leads.db'}",
    )


@pytest.fixture
async def running_app(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def client(running_app):
    async with AsyncClient(transport=ASGITransport(app=running_app), base_url="http://test") as client:
        yield client
