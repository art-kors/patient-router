"""Хранение неизменяемых снимков матрицы и истории оценок в БД."""

import asyncio
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    insert,
    select,
    text,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.services.decision.matrix import MATRIX_PATH, MatrixError, _build, load_triggers, validate
from app.services.decision.thresholds import supports_threshold
from app.settings import get_settings

metadata = MetaData()
versions = Table(
    "routing_matrix_version",
    metadata,
    Column("version", Integer, primary_key=True),
    Column("author", String(255), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("description", String, nullable=False),
    Column("triggers", JSON, nullable=False),
)
snapshots = Table(
    "routing_quality_snapshot",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("split", String(32), nullable=False),
    Column("matrix_version", Integer, nullable=False),
    Column("decoder", String(255), nullable=False),
    Column("metrics", JSON, nullable=False),
)


def check_items(items):
    """Отделить ошибки конфигурации от допустимых врачебных предупреждений."""
    if not isinstance(items, list) or not items:
        raise MatrixError("Матрица должна содержать хотя бы один триггер")
    seen = set()
    triggers = []
    for item in items:
        if not isinstance(item, dict):
            raise MatrixError("Каждый триггер должен быть объектом")
        identifier = item.get("trigger_id")
        if not identifier or identifier in seen or not item.get("display_name"):
            raise MatrixError("Нужны уникальный trigger_id и непустое название")
        seen.add(identifier)
        try:
            trigger = _build(item)
        except (TypeError, ValueError) as exc:
            raise MatrixError(f"{identifier}: некорректные значения полей") from exc
        if not 1 <= trigger.priority <= 4 or trigger.target_sla_days < 1:
            raise MatrixError(f"{identifier}: приоритет от 1 до 4, SLA — положительное число дней")
        if not trigger.display_name.strip():
            raise MatrixError(f"{identifier}: название не может быть пустым")
        if not trigger.synonyms or any(
            not isinstance(s, str) or not s.strip() for s in trigger.synonyms
        ):
            raise MatrixError(f"{identifier}: укажите непустые синонимы")
        if any(not isinstance(s, str) or not s.strip() for s in trigger.negative_contexts):
            raise MatrixError(f"{identifier}: отрицания должны быть непустыми строками")
        if trigger.emergency_flag and trigger.priority != 1:
            raise MatrixError(f"{identifier}: экстренный триггер должен иметь приоритет 1")
        for key, value in trigger.thresholds.items():
            if (
                not supports_threshold(key)
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise MatrixError(f"{identifier}: неподдерживаемый или некорректный порог {key}")
        triggers.append(trigger)
    return validate(triggers)


class MatrixStore:
    """Снимки хранятся целиком; откат создаёт новую версию и сохраняет историю."""

    def __init__(self, session):
        self.session = session

    async def prepare(self):
        """Создать собственные таблицы без изменения моделей и существующих таблиц."""
        if self.session.bind.dialect.name == "postgresql":
            # Эта же блокировка защищает одновременное создание таблиц.
            await self.session.execute(text("SELECT pg_advisory_xact_lock(73641928)"))
        connection = await self.session.connection()
        await connection.run_sync(metadata.create_all)

    async def history(self):
        rows = await self.session.execute(select(versions).order_by(versions.c.version.desc()))
        return [dict(row) for row in rows.mappings()]

    async def current(self):
        rows = await self.session.execute(
            select(versions.c.triggers).order_by(versions.c.version.desc()).limit(1)
        )
        current = rows.scalar_one_or_none()
        if current is not None:
            # Старые снимки тоже показывают пороги, которые реально проверит движок.
            return [dict(item, thresholds=_build(item).thresholds) for item in current]
        return [dict(asdict(t), enabled=True) for t in load_triggers(MATRIX_PATH)]

    async def save(self, items, author, description):
        warnings = check_items(items)
        await self.prepare()
        # Транзакционная блокировка сериализует изменения даже в разных процессах.
        if self.session.bind.dialect.name == "postgresql":
            await self.session.execute(text("SELECT pg_advisory_xact_lock(73641928)"))
        history = await self.history()
        if not history:
            baseline = [dict(asdict(t), enabled=True) for t in load_triggers(MATRIX_PATH)]
            await self.session.execute(
                insert(versions).values(
                    version=1,
                    author="system",
                    created_at=datetime.now(UTC),
                    description="Исходная матрица из файла",
                    triggers=baseline,
                )
            )
        number = history[0]["version"] + 1 if history else 2
        items = [
            dict(asdict(_build(item)), enabled=item.get("enabled", True), version=number)
            for item in items
        ]
        await self.session.execute(
            insert(versions).values(
                version=number,
                author=author,
                created_at=datetime.now(UTC),
                description=description,
                triggers=items,
            )
        )
        await self.session.commit()
        return {"version": number, "warnings": warnings}


async def _read_database():
    """Отдельный пул исключает перенос асинхронных соединений между циклами."""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                select(versions.c.triggers).order_by(versions.c.version.desc()).limit(1)
            )
            return result.scalar_one_or_none()
    finally:
        await engine.dispose()


def database_triggers(*, include_disabled=False):
    """Синхронный мост для прежнего API; пустая или недоступная БД оставляет файл."""

    def read():
        try:
            return asyncio.run(asyncio.wait_for(_read_database(), timeout=2))
        except (SQLAlchemyError, OSError, TimeoutError):
            return None

    with ThreadPoolExecutor(max_workers=1) as executor:
        items = executor.submit(read).result()
    if items is None:
        return None
    check_items(items)
    if include_disabled:
        return [_build(item) for item in items]
    triggers = [_build(item) for item in items if item.get("enabled", True)]
    from app.services.decision.engine import refresh_dictionary

    refresh_dictionary(triggers)
    return triggers
