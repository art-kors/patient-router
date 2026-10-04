"""Проверка реальных миграций на пустой PostgreSQL, без create_all и эталонного SQL."""

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import Response
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.health import ready
from app.schema_check import migration_heads, schema_errors
from app.settings import Settings

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
async def migration_database():
    """Создать отдельную БД; рабочие данные стенда не затрагиваются."""
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Для проверки миграций нужен TEST_DATABASE_URL с правом CREATE DATABASE")
    url = make_url(dsn).set(drivername="postgresql+asyncpg")
    name = f"test_migrations_{uuid4().hex}"
    admin = create_async_engine(url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    database_url = url.set(database=name)
    engine = create_async_engine(database_url)
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    env.update(
        POSTGRES_HOST=url.query.get("host", url.host or "localhost"),
        POSTGRES_PORT=str(url.port or 5432),
        POSTGRES_USER=url.username or "postgres",
        POSTGRES_PASSWORD=url.password or "",
        POSTGRES_DB=name,
        ALEMBIC_TARGET_SCHEMA="",
    )
    try:
        yield engine, env
    finally:
        await engine.dispose()
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        await admin.dispose()


def upgrade(env):
    """Запустить установленный Alembic так же, как при запуске контейнера."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


async def test_upgrade_head_creates_22_tables(migration_database):
    """Ревизия head обязана соответствовать двадцати двум реально созданным таблицам."""
    engine, env = migration_database
    result = upgrade(env)
    assert result.returncode == 0, result.stdout + result.stderr
    async with engine.connect() as connection:
        tables = await connection.run_sync(lambda sync: inspect(sync).get_table_names("public"))
        domain_tables = set(tables) - {"alembic_version"}
        assert len(domain_tables) == 22, f"Ожидалось 22 таблицы, найдено {len(domain_tables)}"
        assert await connection.run_sync(schema_errors) == []


@pytest.mark.parametrize("state", ["empty", "head_without_tables", "missing_table", "old_revision"])
async def test_ready_rejects_broken_schema(migration_database, state):
    """Доступная БД с неправильной схемой должна давать понятный отказ 503."""
    engine, env = migration_database
    if state in {"missing_table", "old_revision"}:
        result = upgrade(env)
        assert result.returncode == 0, result.stderr
    async with engine.begin() as connection:
        if state == "head_without_tables":
            await connection.execute(text("CREATE TABLE alembic_version (version_num varchar(32))"))
            await connection.execute(
                text("INSERT INTO alembic_version VALUES (:head)"),
                {"head": next(iter(migration_heads()))},
            )
        elif state == "missing_table":
            await connection.execute(text("DROP TABLE clinic CASCADE"))
        elif state == "old_revision":
            await connection.execute(
                text("UPDATE alembic_version SET version_num = 'e18a439f9a84'")
            )
    async with async_sessionmaker(engine)() as session:
        response = Response()
        body = await ready(response, session, Settings())
    assert response.status_code == 503
    assert body["database"] == "up"
    assert "отсутствуют таблицы" in body["error"] or "не соответствует head" in body["error"]
    if state == "head_without_tables":
        result = upgrade(env)
        assert result.returncode != 0
        assert "Проверка схемы после миграций" in result.stderr


async def test_seed_creates_89_protocols_and_is_idempotent(
    migration_database, monkeypatch, tmp_path
):
    """Сид работает с настоящими файлами и БД; повторный запуск не дублирует протоколы."""
    from scripts import seed_demo

    engine, env = migration_database
    result = upgrade(env)
    assert result.returncode == 0, result.stderr
    monkeypatch.setattr(seed_demo, "SessionFactory", async_sessionmaker(engine, autoflush=False))
    index_path = tmp_path / "demo" / "study_index.json"
    monkeypatch.setattr(seed_demo, "INDEX_PATH", index_path)
    for _ in range(2):
        assert await seed_demo.main() == 0
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM protocol")) == 89
            assert await connection.scalar(text("SELECT count(*) FROM study")) == 89
    assert index_path.exists()


async def test_ready_accepts_head_with_all_tables(migration_database):
    """После реальных миграций готовность подтверждается кодом 200."""
    engine, env = migration_database
    result = upgrade(env)
    assert result.returncode == 0, result.stderr
    async with async_sessionmaker(engine)() as session:
        response = Response()
        body = await ready(response, session, Settings())
    assert response.status_code == 200
    assert body["status"] == "ready"
