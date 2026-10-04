"""Строгая сверка таблиц рабочей схемы с метаданными ORM для CI."""

import asyncio

from sqlalchemy import inspect
from sqlalchemy.engine import Connection

from app.models import Base


def check_schema(connection: Connection, schema: str = "public") -> str:
    """Сверить имена и количество; служебная таблица Alembic не относится к ORM."""
    expected = {table.name for table in Base.metadata.tables.values()}
    actual = set(inspect(connection).get_table_names(schema=schema)) - {"alembic_version"}
    message = f"Схема {schema}: ожидается {len(expected)} таблиц по ORM, найдено {len(actual)}"
    if actual != expected:
        missing = ", ".join(sorted(expected - actual)) or "нет"
        extra = ", ".join(sorted(actual - expected)) or "нет"
        raise RuntimeError(f"{message}; отсутствуют: {missing}; лишние: {extra}")
    return message


async def main() -> int:
    """Проверить БД приложения и вернуть ненулевой код при расхождении."""
    from app.db import engine

    try:
        async with engine.connect() as connection:
            print(await connection.run_sync(check_schema))
        return 0
    except RuntimeError as exc:
        print(f"Ошибка проверки схемы: {exc}")
        return 1
    finally:
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
