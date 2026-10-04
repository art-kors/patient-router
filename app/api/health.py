"""Служебные ручки: проверка живости и готовности БД."""

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import get_clock
from app.db import get_session
from app.schema_check import schema_errors
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
    """Готовность: БД доступна, миграции на head и доменные таблицы существуют."""
    try:
        errors = await session.run_sync(
            lambda sync_session: schema_errors(
                sync_session.connection(), settings.alembic_target_schema or "public"
            )
        )
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready", "database": "down", "error": "Не удалось проверить схему БД"}

    if errors:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready", "database": "up", "error": "; ".join(errors)}

    return {
        "status": "ready",
        "database": "up",
        "environment": settings.environment,
    }
