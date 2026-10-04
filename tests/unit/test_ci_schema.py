"""Саботаж настоящего каталога БД: проверка CI обязана замечать расхождения."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, text

from app.models import Base
from scripts.check_schema import check_schema, main


@pytest.fixture
def schema_connection():
    """Создать каталог с именами ORM без специфичных для PostgreSQL типов и DEFAULT."""
    engine = create_engine("sqlite://")
    metadata = MetaData()
    for table in Base.metadata.tables.values():
        Table(table.name, metadata, Column("id", Integer))
    Table("alembic_version", metadata, Column("version_num", Integer))
    metadata.create_all(engine)
    with engine.connect() as connection:
        yield connection
    engine.dispose()


def test_schema_matches_orm(schema_connection):
    """Служебная таблица не учитывается, ожидание вычисляется из актуального ORM."""
    message = check_schema(schema_connection, schema="main")
    assert f"ожидается {len(Base.metadata.tables)} таблиц по ORM" in message


@pytest.mark.parametrize("sabotage", ["missing", "extra", "replacement", "empty"])
async def test_sabotage_fails_check_and_cli(schema_connection, monkeypatch, sabotage, capsys):
    """Удаление, добавление и подмена при том же числе таблиц дают отказ и код 1."""
    if sabotage in {"missing", "replacement"}:
        schema_connection.execute(text("DROP TABLE analysis_feedback"))
    if sabotage in {"extra", "replacement"}:
        schema_connection.execute(text("CREATE TABLE unexpected_table (id INTEGER)"))
    if sabotage == "empty":
        for table in Base.metadata.tables.values():
            schema_connection.execute(text(f'DROP TABLE "{table.name}"'))
    with pytest.raises(RuntimeError) as error:
        check_schema(schema_connection, schema="main")
    message = str(error.value)
    assert "ожидается" in message and "найдено" in message
    if sabotage in {"missing", "replacement", "empty"}:
        assert "analysis_feedback" in message.split("; лишние:")[0]
    if sabotage in {"extra", "replacement"}:
        assert "лишние: unexpected_table" in message

    # CLI выполняет ту же проверку на повреждённом каталоге и возвращает код ошибки.
    from app import db

    engine = AsyncMock()
    connection = AsyncMock()
    connection.run_sync.side_effect = lambda check: check(schema_connection, schema="main")
    engine.connect = lambda: connection
    connection.__aenter__.return_value = connection
    monkeypatch.setattr(db, "engine", engine)
    assert await main() == 1
    assert message in capsys.readouterr().out
    engine.dispose.assert_awaited_once()


def test_new_orm_table_changes_expectation(schema_connection):
    """Новая модель сразу меняет ожидание; правка числа в CI не требуется."""
    table = Table("future_model", Base.metadata, Column("id", Integer))
    try:
        with pytest.raises(RuntimeError, match="отсутствуют: future_model"):
            check_schema(schema_connection, schema="main")
        table.create(schema_connection)
        check_schema(schema_connection, schema="main")
    finally:
        Base.metadata.remove(table)
