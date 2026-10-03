"""Точка входа FastAPI."""

import logging
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.api.health import router as health_router
from app.settings import get_settings

settings = get_settings()

logging.basicConfig(level=settings.log_level)
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        logging.DEBUG if settings.debug else logging.INFO
    ),
)
log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Старт и корректное завершение приложения."""
    log.info(
        "startup",
        app=settings.app_name,
        environment=settings.environment,
        db=settings.postgres_db,
    )
    yield
    # закрываем пул соединений, иначе контейнер будет висеть на завершении
    from app.db import dispose_engine

    await dispose_engine()
    log.info("shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="patient-router",
        description=(
            "СМ-Клиника · маршрутизация пациентов по результатам УЗИ. "
            "Система не ставит диагноз и не назначает лечение."
        ),
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
    )
    app.include_router(health_router, tags=["service"])
    return app


app = create_app()