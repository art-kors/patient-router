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
