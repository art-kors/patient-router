"""Приём событий МИС и журнал обработки."""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import MisEvent
from app.services.mis import EVENT_TYPES, MisEventHandler

router = APIRouter(prefix="/api/v1/mis", tags=["МИС"])


class EventSubject(BaseModel):
    patient_id: UUID | None = None
    study_id: UUID | None = None
    route_id: UUID | None = None


class EventIn(BaseModel):
    event_id: str = Field(
        min_length=1, max_length=128, description="Уникальный идентификатор доставки"
    )
    event_type: str = Field(min_length=1, max_length=64, description="Тип события МИС")
    occurred_at: AwareDatetime
    subject: EventSubject
    payload: dict[str, Any] = Field(default_factory=dict)


class EventOut(BaseModel):
    event_id: str
    event_type: str
    occurred_at: datetime
    processed_at: datetime | None
    patient_id: UUID | None
    study_id: UUID | None
    payload: dict[str, Any]


@router.post("/events", status_code=202, summary="Принять событие МИС")
async def receive_event(
    event: EventIn, response: Response, session: Annotated[AsyncSession, Depends(get_session)]
) -> dict:
    result = await MisEventHandler().handle(session, event.model_dump(mode="json"))
    await session.commit()
    if result["duplicate"]:
        response.status_code = 200
    return result


@router.get("/events", response_model=list[EventOut], summary="Журнал входящих событий МИС")
async def list_events(
    session: Annotated[AsyncSession, Depends(get_session)],
    event_type: str | None = None,
    processed: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    query = select(MisEvent)
    if event_type:
        query = query.where(MisEvent.event_type == event_type)
    if processed is not None:
        query = query.where(
            MisEvent.processed_at.is_not(None) if processed else MisEvent.processed_at.is_(None)
        )
    rows = (
        await session.scalars(
            query.order_by(MisEvent.occurred_at.desc(), MisEvent.id).offset(offset).limit(limit)
        )
    ).all()
    return [EventOut.model_validate(row, from_attributes=True) for row in rows]


@router.get("/event-types", summary="Справочник поддерживаемых событий МИС")
async def event_types() -> list[dict[str, str]]:
    return [
        {"event_type": name, "description": description}
        for name, description in EVENT_TYPES.items()
    ]
