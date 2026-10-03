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

    # --- конфигурация ---
    # Каталог с routing_matrix.json и прочими настройками.
    # В контейнере — /app/config, локально — ./config относительно корня.
    config_dir: str = "config"
    labeled_data_dir: str = "data/labeled"
    study_index_path: str = "data/demo/study_index.json"

    @property
    def routing_matrix_path(self) -> str:
        """Путь к матрице маршрутизации."""
        return f"{self.config_dir}/routing_matrix.json"

    # --- миграции ---
    # пусто = использовать схему из DSN (в docker это public).
    # Нужно для локальной проверки миграций в отдельной схеме.
    alembic_target_schema: str = ""

    # --- лимиты ---
    max_notifications_per_route: int = 8
    notification_throttle_seconds: int = 3600

    @property
    def database_url(self) -> str:
        """DSN для asyncpg.

        Если postgres_host — путь к сокету (начинается с /), подключаемся
        через unix-сокет: пароль не нужен, работает peer-авторизация.
        """
        user = self.postgres_user
        if self.postgres_password:
            user = f"{user}:{self.postgres_password}"

        if self.postgres_host.startswith("/"):
            # unix-сокет: host передаётся параметром, а не частью authority
            return f"postgresql+asyncpg://{user}@/{self.postgres_db}?host={self.postgres_host}"

        return (
            f"postgresql+asyncpg://{user}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def is_production(self) -> bool:
        return self.environment in {"prod", "production"}


@lru_cache
def get_settings() -> Settings:
    """Настройки синглтон (кэш, чтобы не читать .env на каждый запрос)."""
    return Settings()
