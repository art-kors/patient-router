"""Готовность не должна подтверждать пустую или повреждённую схему."""

from unittest.mock import AsyncMock

import pytest
from fastapi import Response
from sqlalchemy.exc import ProgrammingError

from app.api.health import ready
from app.models import Base
from app.settings import Settings


@pytest.mark.anyio
async def test_пустая_схема_при_доступной_бд():
    """Даже успешный SELECT 1 не означает, что миграции создали таблицы."""
    session = AsyncMock()
    session.execute.side_effect = [
        None,
        ProgrammingError("SELECT", {}, Exception('relation "specialty" does not exist')),
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
    response = Response()
    result = await ready(response, session, Settings())
    assert response.status_code == 200
    assert result["schema"] == "up"
    queries = [call.args[0] for call in session.execute.call_args_list[1:]]
    assert len(queries) == len(Base.metadata.tables)
    for query, table in zip(queries, Base.metadata.sorted_tables, strict=True):
        assert list(query.selected_columns) == list(table.columns)
        assert query.get_final_froms() == [table]
