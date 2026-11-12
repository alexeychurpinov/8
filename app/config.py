from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    api_key: SecretStr
    telegram_bot_token: SecretStr = SecretStr("")
    admin_telegram_id: int = Field(default=0, ge=0)
    database_url: str = "sqlite+aiosqlite:///./data/leads.db"
    rate_limit_per_minute: int = Field(default=20, ge=1, le=10000)
    auth_rate_limit_per_minute: int = Field(default=120, ge=1, le=10000)
    telegram_enabled: bool = True

    @field_validator("api_key")
    @classmethod
    def strong_key(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32 or value.get_secret_value().startswith("replace-"):
            raise ValueError("API_KEY must be a random secret of at least 32 characters")
        return value

    @field_validator("database_url")
    @classmethod
    def sqlite_only(cls, value: str) -> str:
        if not value.startswith("sqlite+aiosqlite:///"):
            raise ValueError("DATABASE_URL must use sqlite+aiosqlite")
        return value

    @model_validator(mode="after")
    def telegram_credentials(self):
        if self.telegram_enabled and (
            self.admin_telegram_id <= 0
            or not self.telegram_bot_token.get_secret_value()
            or "replace-with" in self.telegram_bot_token.get_secret_value()
        ):
            raise ValueError("Set TELEGRAM_BOT_TOKEN and ADMIN_TELEGRAM_ID")
        return self
