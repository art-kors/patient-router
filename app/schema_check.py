"""Проверка ревизии и наличия доменных таблиц в рабочей схеме."""

from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Connection

from app.models import Base

REPO_ROOT = Path(__file__).resolve().parents[1]


def migration_heads() -> set[str]:
    """Получить ожидаемые ревизии из миграций, поставленных с приложением."""
    config = Config(str(REPO_ROOT / "alembic.ini"))
    return set(ScriptDirectory.from_config(config).get_heads())


def schema_errors(connection: Connection, schema: str = "public") -> list[str]:
    """Проверить именно рабочую схему, даже если версия уже отмечена как head."""
    tables = set(inspect(connection).get_table_names(schema=schema))
    errors = []
    missing = set(Base.metadata.tables) - tables
    if missing:
        errors.append(f"В схеме {schema} отсутствуют таблицы: {', '.join(sorted(missing))}")
    current = set(
        MigrationContext.configure(
            connection, opts={"version_table_schema": schema}
        ).get_current_heads()
    )
    expected = migration_heads()
    if current != expected:
        errors.append(
            f"Ревизия Alembic не соответствует head: "
            f"текущая {sorted(current)}, ожидаемая {sorted(expected)}"
        )
    return errors
