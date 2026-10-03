"""Конфигурация приложения из переменных окружения."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки. Значения читаются из окружения и .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- приложение ---
    app_name: str = "patient-router"
    environment: str = "dev"
    debug: bool = True
    log_level: str = "INFO"

    # --- БД ---
    postgres_host: str = "db"
    postgres_port: int = 5432
    postgres_db: str = "patient_router"
    postgres_user: str = "pr"
    postgres_password: str = Field(default="pr_local_dev", repr=False)

    # --- время ---
    timezone: str = "Europe/Moscow"
    use_model_clock: bool = False

    # --- лимиты ---
    max_notifications_per_route: int = 8
    notification_throttle_seconds: int = 3600

    @property
    def database_url(self) -> str:
        """DSN для asyncpg."""
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def is_production(self) -> bool:
        return self.environment in {"prod", "production"}


@lru_cache
def get_settings() -> Settings:
    """Настройки синглтон (кэш, чтобы не читать .env на каждый запрос)."""
    return Settings()