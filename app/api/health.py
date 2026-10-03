"""Служебные ручки: проверка живости и готовности БД."""

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import get_clock
from app.db import get_session
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
    """Готовность: приложение живо И БД отвечает."""
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready", "database": "down", "error": str(exc)[:200]}

    return {
        "status": "ready",
        "database": "up",
        "environment": settings.environment,
    }
