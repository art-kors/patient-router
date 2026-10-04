"""Готовность не должна подтверждать пустую или повреждённую схему."""

from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import Response
from sqlalchemy.exc import ProgrammingError

from app.api.health import ready
from app.models import Base
from app.settings import Settings


@pytest.mark.anyio
@pytest.mark.parametrize(
    "defect",
    ['relation "specialty" does not exist', 'column "name" does not exist'],
)
async def test_пустая_или_повреждённая_схема_при_доступной_бд(defect):
    """Даже успешный SELECT 1 не означает, что миграции создали таблицы."""
    session = AsyncMock()
    session.run_sync.return_value = []
    session.execute.side_effect = [
        None,
        ProgrammingError("SELECT", {}, Exception(defect)),
    ]
    response = Response()
    result = await ready(response, session, Settings())
    assert response.status_code == 503
    assert result["database"] == "up"
    assert result["schema"] == "unavailable"


@pytest.mark.anyio
async def test_проверяются_все_таблицы_и_колонки():
    """Повреждение любой таблицы должно обнаруживаться до ответа ready."""
    session = AsyncMock()
    session.run_sync.return_value = []
    response = Response()
    result = await ready(response, session, Settings())
    assert response.status_code == 200
    assert result["schema"] == "up"
    queries = [call.args[0] for call in session.execute.call_args_list[1:]]
    assert len(queries) == len(Base.metadata.tables)
    for query, table in zip(queries, Base.metadata.sorted_tables, strict=True):
        assert list(query.selected_columns) == list(table.columns)
        assert query.get_final_froms() == [table]
        assert query._limit_clause.value == 0


@pytest.mark.anyio
async def test_недоступная_бд_не_проверяет_схему():
    """Отказ SELECT 1 должен завершать проверку с кодом 503."""
    session = AsyncMock()
    session.execute.side_effect = OSError("Секретные сведения о подключении")
    response = Response()
    result = await ready(response, session, Settings())
    assert response.status_code == 503
    assert result["database"] == "down"
    assert result["error"] == "БД недоступна"
    session.run_sync.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "errors",
    [
        ["В схеме public отсутствуют таблицы: clinic"],
        ["Ревизия Alembic не соответствует head"],
    ],
)
async def test_дефекты_миграций_и_таблиц_дают_503(errors):
    """Доступность БД не компенсирует отсутствие таблиц или миграций."""
    session = AsyncMock()
    session.run_sync.return_value = errors
    response = Response()
    result = await ready(response, session, Settings())
    assert response.status_code == 503
    assert result["database"] == "up"
    assert result["schema"] == "unavailable"
    assert result["error"] == "; ".join(errors)
    session.execute.assert_awaited_once()


@pytest.mark.anyio
async def test_проверка_схемы_использует_рабочую_схему(monkeypatch):
    """Проверка Alembic получает подключение и выбранную схему."""
    check = Mock(return_value=[])
    monkeypatch.setattr("app.api.health.schema_errors", check)
    sync_session = Mock()
    session = AsyncMock()
    session.run_sync.side_effect = lambda callback: callback(sync_session)
    response = Response()
    result = await ready(response, session, Settings(alembic_target_schema="custom"))
    assert response.status_code == 200
    assert result["schema"] == "up"
    check.assert_called_once_with(sync_session.connection.return_value, "custom")


@pytest.mark.anyio
async def test_ошибка_проверки_миграций_даёт_503():
    """Сбой инспекции схемы не должен подтверждать готовность приложения."""
    session = AsyncMock()
    session.run_sync.side_effect = RuntimeError("Секретные сведения")
    response = Response()
    result = await ready(response, session, Settings())
    assert response.status_code == 503
    assert result["database"] == "up"
    assert result["schema"] == "unavailable"
    assert result["error"] == "Не удалось проверить миграции и таблицы БД"
