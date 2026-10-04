"""Демонстрационные ручки внешней МИС; роутер подключает координатор."""

from datetime import date
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.services.mis import MisEventConflictError
from app.services.mock_mis import MockMisService

router = APIRouter(prefix="/api/v1/mock/mis", tags=["Демо-МИС"])


class EmitIn(BaseModel):
    """Выбор документа или пациента и факты внешней системы."""

    study_id: str | None = Field(None, description="Демо-ID исследования или UUID из сида")
    patient_id: UUID | None = Field(None, description="UUID пациента из каталога")
    route_id: UUID | None = Field(None, description="Маршрут; по умолчанию последний у пациента")
    event_id: str | None = Field(
        None,
        min_length=1,
        max_length=128,
        description="ID доставки; по умолчанию новый уникальный ID",
    )
    occurred_at: AwareDatetime | None = Field(None, description="Время факта с часовым поясом")
    payload: dict[str, Any] = Field(default_factory=dict, description="Факты МИС, включая тактику")


class SignedIn(EmitIn):
    """Подписанный документ из демо-каталога."""

    study_id: str = Field(min_length=1, description="Демо-ID исследования или UUID из сида")


class StudyOut(BaseModel):
    """Документ внешней системы без клинической разметки."""

    study_id: str = Field(description="Идентификатор документа МИС")
    id: UUID = Field(description="UUID исследования, совместимый с демо-сидом")
    study_type: str = Field(description="Тип исследования из заголовка")
    study_date: date = Field(description="Дата исследования из документа")
    text: str = Field(description="Полный исходный синтетический протокол")


class PatientOut(BaseModel):
    """Синтетический пациент и доступные документы."""

    patient_id: UUID = Field(description="UUID пациента, совместимый с демо-сидом")
    external_id: str = Field(description="Синтетический номер амбулаторной карты")
    name: str = Field(description="Синтетическое имя из документа")
    age: int = Field(description="Возраст на момент исследования")
    sex: str | None = Field(description="Пол: M, F или не указан")
    studies: list[StudyOut] = Field(description="Исследования пациента без оценки триггеров")


def get_mock_mis() -> MockMisService:
    """Создать адаптер файлового каталога без общего изменяемого состояния."""
    return MockMisService()


Service = Annotated[MockMisService, Depends(get_mock_mis)]
Session = Annotated[AsyncSession, Depends(get_session)]


def translate_error(exc: Exception) -> HTTPException:
    """Перевести ожидаемые ошибки каталога и доставки в HTTP."""
    if isinstance(exc, MisEventConflictError):
        return HTTPException(409, detail={"code": "EVENT_ID_CONFLICT", "message": str(exc)})
    if isinstance(exc, FileNotFoundError):
        return HTTPException(503, detail=str(exc))
    return HTTPException(404 if isinstance(exc, LookupError) else 422, detail=str(exc))


@router.get("/patients", response_model=list[PatientOut], summary="Каталог пациентов внешней МИС")
async def patients(service: Service):
    """Показать все документы, включая нормальные исследования."""
    try:
        return service.catalog()
    except (FileNotFoundError, ValueError) as exc:
        raise translate_error(exc) from exc


async def deliver(event_type: str, body: EmitIn, service: MockMisService, session):
    """Обработать доставку и откатить её при отказе."""
    try:
        return await service.emit(session, event_type, body)
    except (FileNotFoundError, ValueError, LookupError) as exc:
        await session.rollback()
        raise translate_error(exc) from exc


@router.post("/emit", summary="Передать подписанный протокол исследования")
async def emit(body: SignedIn, service: Service, session: Session):
    """Вернуть реальные действия patient-router после передачи документа."""
    return await deliver("StudyProtocolSigned", body, service, session)


@router.post("/emit/{event_type}", summary="Передать одно из 12 событий МИС")
async def emit_event(event_type: str, body: EmitIn, service: Service, session: Session):
    """Передать факт с автоматическим выбором последнего маршрута пациента."""
    return await deliver(event_type, body, service, session)


@router.get("/queue", summary="Документы к передаче, журнал и находки patient-router")
async def queue(
    service: Service, session: Session, limit: Annotated[int, Query(ge=1, le=500)] = 100
):
    """Показать состояние обмена и обратную связь принимающей системы."""
    try:
        return await service.queue(session, limit)
    except (FileNotFoundError, ValueError) as exc:
        raise translate_error(exc) from exc
