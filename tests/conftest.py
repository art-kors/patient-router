"""Общие фикстуры.

Здесь важно: тесты не должны требовать запущенной БД. Всё, что работает без
PostgreSQL, проверяется на SQLite/in-memory или моках; интеграционные тесты с
реальной БД помечены маркером ``integration`` и пропускаются без переменной
окружения ``TEST_DATABASE_URL``.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from app.clock import ModelClock, set_clock


@pytest.fixture
def anyio_backend() -> str:
    """httpx.AsyncClient работает на asyncio."""
    return "asyncio"


@pytest.fixture
def model_clock() -> Iterator[ModelClock]:
    """Модельное время на фиксированной точке старта сценариев."""
    clock = ModelClock(start=datetime(2026, 8, 26, 14, 32, tzinfo=UTC))
    set_clock(clock)
    yield clock
    set_clock(None)


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """HTTP-клиент против приложения без поднятия uvicorn."""
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac.headers.update(auth_headers())
        yield ac


@pytest.fixture
def event_loop_policy():
    """Явная политика цикла событий."""
    return asyncio.get_event_loop_policy()


def pytest_collection_modifyitems(items):
    """Интеграционные тесты помечаем маркером — их можно исключить."""
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)


@pytest.fixture
def analytics_session():
    """Настоящие SQL-запросы журнала на изолированной SQLite без сервера БД."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.models import AnalysisFeedback, AnalysisRun, Study

    class SessionAdapter:
        """Минимальный асинхронный интерфейс над синхронной тестовой сессией."""

        def __init__(self):
            self.bind = create_engine("sqlite://")
            for model in (AnalysisRun, AnalysisFeedback, Study):
                model.__table__.create(self.bind)
            self.sync = Session(self.bind, expire_on_commit=False)

        async def execute(self, stmt):
            return self.sync.execute(stmt)

        async def scalar(self, stmt):
            return self.sync.scalar(stmt)

        async def scalars(self, stmt):
            return self.sync.scalars(stmt)

        async def get(self, model, identifier):
            return self.sync.get(model, identifier)

        async def commit(self):
            self.sync.commit()

        async def rollback(self):
            self.sync.rollback()

    session = SessionAdapter()
    yield session
    session.sync.close()
    session.bind.dispose()


def auth_headers(role="admin", patient_ids=None):
    """Подписанный вход для тестов API без обхода серверных прав."""
    import time

    from app.auth import sign

    return {
        "Authorization": "Bearer "
        + sign(
            {"sub": role, "role": role, "patient_ids": patient_ids or [], "exp": time.time() + 3600}
        )
    }
