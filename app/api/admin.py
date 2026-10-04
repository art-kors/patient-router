"""API настроек маршрутизации для врачебного дашборда."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from app.db import get_session
from app.services.admin import AdminService
from app.services.decision.engine import reload_engine

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


class AuditInput(BaseModel):
    """Автор и описание изменения для журнала."""

    model_config = ConfigDict(extra="forbid")
    author: str = Field(default="Администратор", min_length=1, max_length=255)
    description: str = ""


class TriggerInput(AuditInput):
    """Поля правила; PUT допускает частичное изменение."""

    display_name: str | None = Field(default=None, min_length=1)
    source_study: str | None = None
    synonyms: list[str] | None = None
    negative_contexts: list[str] | None = None
    thresholds: dict[str, Annotated[float, Field(strict=True, allow_inf_nan=False)]] | None = None
    specialty: str | None = None
    potential_route: str | None = None
    department: str | None = None
    target_sla_days: int | None = Field(default=None, ge=1)
    priority: int | None = Field(default=None, ge=1, le=4)
    emergency_flag: bool | None = None
    enabled: bool | None = None

    def patch(self):
        if any(value is None for value in self.model_dump(exclude_unset=True).values()):
            raise HTTPException(422, "Поля триггера не могут быть null")
        return self.model_dump(exclude_unset=True, exclude={"author", "description"})


class NewTrigger(TriggerInput):
    """Новый триггер требует идентификатор и название."""

    trigger_id: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9_-]+$")
    display_name: str = Field(min_length=1)
    synonyms: list[str] = Field(min_length=1)


async def service(session=Depends(get_session)):
    """Сессия заменяется через dependency_overrides в тестах."""
    try:
        yield AdminService(session)
    except (SQLAlchemyError, OSError) as exc:
        raise HTTPException(503, "БД настроек недоступна") from exc


Service = Annotated[AdminService, Depends(service)]


@router.get("/triggers")
async def triggers(admin: Service):
    """Все правила, включая отключённые."""
    return await admin.items()


@router.get("/triggers/{trigger_id}")
async def trigger(trigger_id: str, admin: Service):
    """Полная конфигурация одного правила."""
    return await admin.get(trigger_id)


@router.put("/triggers/{trigger_id}")
async def update(trigger_id: str, body: TriggerInput, admin: Service):
    """Сохранить и сразу применить изменения."""
    return await admin.change(trigger_id, body.patch(), body.author, body.description)


@router.post("/triggers", status_code=201)
async def create(body: NewTrigger, admin: Service):
    """Добавить правило без изменения кода."""
    return await admin.change(
        body.trigger_id, body.patch(), body.author, body.description, create=True
    )


@router.delete("/triggers/{trigger_id}")
async def disable(
    trigger_id: str,
    admin: Service,
    author: str = "Администратор",
    description: str = "Отключение триггера",
):
    """Мягко отключить правило, сохранив его в истории."""
    return await admin.change(trigger_id, {"enabled": False}, author, description)


@router.get("/versions")
async def history(admin: Service):
    """История снимков без объёмного содержимого матрицы."""
    await admin.store.prepare()
    return [
        {k: v for k, v in row.items() if k != "triggers"} for row in await admin.store.history()
    ]


@router.post("/versions/{version}/rollback")
async def rollback(version: int, body: AuditInput, admin: Service):
    """Откатить матрицу новой записью журнала."""
    return await admin.rollback(version, body.author, body.description)


@router.get("/validate")
async def warnings(admin: Service):
    """Предупреждения для проверки врачом."""
    return {"warnings": await admin.warnings()}


@router.post("/reload")
def reload():
    """Перезагрузить актуальную матрицу и словарный декодер."""
    try:
        return {"applied": True, "triggers": len(reload_engine())}
    except SQLAlchemyError as exc:
        raise HTTPException(503, "БД конфигурации недоступна") from exc
