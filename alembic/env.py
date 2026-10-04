"""Alembic env: конфигурация из app.settings, async-движок.

Ключевое: URL берётся из ``Settings.database_url``, а не из alembic.ini —
чтобы миграции и приложение всегда ходили в одну и ту же БД.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from app.models import Base
from app.schema_check import migration_heads, schema_errors
from app.settings import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()

# autogenerate сравнивает состояние БД с этими метаданными
target_metadata = Base.metadata

# Непустая схема нужна, чтобы гонять миграции рядом с существующими
# таблицами, не задевая их (локальная разработка и тесты).
# Реализуется через search_path на уровне подключения.
target_schema = settings.alembic_target_schema or None
schema_arg = f"-csearch_path%3D{target_schema}" if target_schema else ""


def _database_url() -> str:
    """DSN для миграций (тот же, что у приложения)."""
    base = settings.database_url
    if not target_schema:
        return base
    return base


# alembic.ini содержит плейсхолдер — подставляем реальный DSN
config.set_main_option("sqlalchemy.url", _database_url())


def run_migrations_offline() -> None:
    """Миграции без подключения к БД: SQL печатается в stdout."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()
        # Отметка head сама по себе не доказывает, что DDL был выполнен.
        if set(context.get_context().get_current_heads()) == migration_heads():
            errors = schema_errors(connection, target_schema or "public")
            if errors:
                raise RuntimeError("Проверка схемы после миграций: " + "; ".join(errors))


async def run_async_migrations() -> None:
    # search_path задаём через server_settings: asyncpg не понимает
    # параметр options в DSN, но принимает server_settings при подключении.
    connect_args = {}
    if target_schema:
        connect_args["server_settings"] = {"search_path": target_schema}

    connectable = create_async_engine(
        _database_url(),
        poolclass=pool.NullPool,
        connect_args=connect_args,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
