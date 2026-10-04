"""Точка входа FastAPI."""

import logging
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.api.admin import router as admin_router
from app.api.analysis import router as analysis_router
from app.api.demo import router as demo_router
from app.api.health import router as health_router
from app.api.mock_lk import router as mock_lk_router
from app.api.mock_mis import router as mock_mis_router
from app.api.mis import router as mis_router
from app.api.quality import router as quality_router
from app.api.routes import router as routes_router
from app.api.ui import router as ui_router
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
    """Старт и корректное завершение приложения.

    Матрица маршрутизации проверяется ЗДЕСЬ, на старте, а не лениво на
    первом запросе. Иначе стенд поднимается, /health отвечает 200, а
    через минуту первый же пациент упирается в «матрица не найдена» —
    и демо выглядит как поломка на защите. Матрица — это данные, которые
    заказчик правит руками, поэтому ловим ошибку заранее и громко.

    Не поднимаемся без матрицы: маршрутизировать пациентов по неполным
    правилам опаснее, чем не подняться вовсе.
    """
    from app.services.decision.matrix import MatrixError, load_triggers, validate

    try:
        triggers = load_triggers()
    except MatrixError as exc:
        # Не поднимаемся: с понятным текстом, а не «матрица не найдена»
        # через минуту после зелёного /health.
        log.error("routing_matrix_unavailable", error=str(exc))
        raise

    for warning in validate(triggers):
        # Предупреждения не мешают работе — но врач должен их видеть.
        log.warning("routing_matrix_warning", detail=warning)

    log.info(
        "startup",
        app=settings.app_name,
        environment=settings.environment,
        db=settings.postgres_db,
        routing_matrix=settings.routing_matrix_path,
        triggers=len(triggers),
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
    app.include_router(demo_router)
    app.include_router(analysis_router)
    app.include_router(routes_router)
    app.include_router(mis_router)
    app.include_router(quality_router)
    app.include_router(admin_router)
    app.include_router(mock_mis_router)
    app.include_router(mock_lk_router)
    app.include_router(ui_router)
    return app


app = create_app()
