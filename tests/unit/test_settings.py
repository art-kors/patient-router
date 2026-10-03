"""Тесты конфигурации.

Здесь же — проверка DSN: в нём легко ошибиться, а ошибка проявится только
в момент подключения к БД.
"""

from pathlib import Path

import pytest

from app.settings import Settings, get_settings


class TestDatabaseUrl:
    def test_tcp_dsn_содержит_хост_и_порт(self):
        s = Settings(
            POSTGRES_HOST="db",
            POSTGRES_PORT=5432,
            POSTGRES_DB="patient_router",
            POSTGRES_USER="pr",
            POSTGRES_PASSWORD="secret",
        )
        url = s.database_url
        assert url.startswith("postgresql+asyncpg://")
        assert "db:5432/patient_router" in url
        assert "secret" in url

    def test_unix_socket_использует_параметр_host(self):
        """Путь вместо хоста → подключение через сокет, пароль не нужен."""
        s = Settings(
            POSTGRES_HOST="/run/postgresql",
            POSTGRES_DB="vzuh",
            POSTGRES_USER="m_danilin",
            POSTGRES_PASSWORD="",
        )
        url = s.database_url
        assert "host=/run/postgresql" in url
        assert "/vzuh" in url
        # важно: порт не должен попасть в путь authority
        assert "@/" in url

    def test_пустой_пароль_не_даёт_двойного_слеша(self):
        s = Settings(POSTGRES_HOST="db", POSTGRES_USER="pr", POSTGRES_PASSWORD="")
        assert "//pr@db" in s.database_url
        assert "pr:@db" not in s.database_url


class TestEnvironment:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [("prod", True), ("production", True), ("dev", False), ("test", False)],
    )
    def test_is_production(self, value: str, expected: bool):
        assert Settings(ENVIRONMENT=value).is_production is expected

    def test_дефолты_безопасные(self):
        s = Settings()
        assert s.max_notifications_per_route > 0
        assert s.notification_throttle_seconds > 0
        assert s.alembic_target_schema == ""


class TestGetSettings:
    def test_кешируется(self):
        assert get_settings() is get_settings()

    def teardown_method(self) -> None:
        get_settings.cache_clear()

    def test_читает_переменные_окружения(self, monkeypatch):
        get_settings.cache_clear()
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        assert get_settings().log_level == "DEBUG"
        get_settings.cache_clear()


class TestEnvFile:
    def test_env_в_гитигноре(self):
        """Пароль БД не должен попасть в репозиторий."""
        gitignore = Path(__file__).resolve().parents[2] / ".gitignore"
        content = gitignore.read_text(encoding="utf-8")
        assert ".env" in content
        assert "!.env.example" in content
