"""Служебные ручки: проверка живости и готовности БД."""

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import get_clock
from app.db import get_session
from app.models import Base
from app.settings import Settings, get_settings

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    """Живость приложения. Не проверяет БД — для этого /ready."""
    clock = get_clock()
    return {
        "status": "ok",
        "app": get_settings().app_name,
        "time": clock.now().isoformat(),
        "model_clock": clock.is_mock(),
    }


@router.get("/ready")
async def ready(
    response: Response,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Готовность: БД отвечает, все таблицы и колонки приложения доступны."""
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready", "database": "down", "error": str(exc)[:200]}

    try:
        # Метка Alembic сама по себе не доказывает наличие схемы.
        # LIMIT 0 проверяет таблицы и колонки без чтения данных пациентов.
        for table in Base.metadata.sorted_tables:
            await session.execute(select(table).limit(0))
    except Exception as exc:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "not_ready",
            "database": "up",
            "schema": "unavailable",
            "error": str(exc)[:200],
        }

    return {
        "status": "ready",
        "schema": "up",
        "database": "up",
        "environment": settings.environment,
    }
