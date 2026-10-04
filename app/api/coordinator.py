"""Очередь координатора, подтверждаемая загрузка и все протоколы пациента."""

from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import or_, select

from app.api.analysis import _analyze, _decode
from app.auth import patient_access, principal, sign, verify
from app.clock import get_clock
from app.db import get_session
from app.models import Finding, Patient, Protocol, Route, Study, TriggerMatch
from app.services.anonymization import anonymize
from app.services.decision.matrix import load_triggers
from app.services.mis import MisEventHandler
from app.services.mock_mis import MockMisService

router = APIRouter(prefix="/api/v1/coordinator", tags=["coordinator"])


@router.get("/study-types")
def study_types():
    """Подсказки из действующей матрицы, без отдельного расходящегося справочника."""
    return sorted({t.source_study for t in load_triggers() if t.source_study})


@router.get("/patients")
async def patients(
    q: str = "",
    sort: Literal["date", "name"] = "date",
    user=Depends(principal),
    session=Depends(get_session),
):
    """Поиск ФИО в демо-МИС, карты и ID исследования; очередь ограничена сервером."""
    catalog = {str(p["patient_id"]): p for p in MockMisService().catalog()}
    ids = [UUID(i) for i in user.get("patient_ids", [])]
    filters = (
        []
        if user["role"] == "admin"
        else [
            or_(
                Patient.id.in_(ids),
                Patient.id.in_(
                    select(Study.patient_id).where(
                        Study.document_ref == "coordinator:" + user["sub"]
                    )
                ),
            )
        ]
    )
    rows = (await session.scalars(select(Patient).where(*filters))).all()
    items = []
    for patient in rows:
        studies = (
            await session.scalars(
                select(Study)
                .where(Study.patient_id == patient.id)
                .order_by(Study.study_date.desc(), Study.id)
            )
        ).all()
        demo = catalog.get(str(patient.id), {})
        name = demo.get("name", "Обезличенный пациент")
        keys = [name, patient.external_id, str(patient.id)]
        keys += [str(s.id) for s in studies] + [s.document_ref or "" for s in studies]
        keys += [s["study_id"] for s in demo.get("studies", [])]
        if q.casefold().strip() not in " ".join(keys).casefold():
            continue
        items.append(
            {
                "id": patient.id,
                "name": name,
                "card": patient.external_id,
                "date": studies[0].study_date if studies else patient.created_at.date(),
                "protocol_count": len(studies),
            }
        )
    items.sort(
        key=lambda p: (
            (p["name"].casefold(), str(p["id"])) if sort == "name" else (p["date"], str(p["id"]))
        ),
        reverse=sort != "name",
    )
    return {
        "items": items,
        "total": len(items),
        "message": ""
        if items
        else (
            "Пациенты не найдены. Измените запрос или загрузите "
            "обезличенный протокол для нового пациента."
        ),
    }


@router.get("/tasks")
async def tasks(user=Depends(principal), session=Depends(get_session)):
    """Только задачи роли координатора и пациентов его очереди."""
    from app.models import Task

    ids = [UUID(i) for i in user.get("patient_ids", [])]
    scope = (
        []
        if user["role"] == "admin"
        else [
            or_(
                Route.patient_id.in_(ids),
                Route.patient_id.in_(
                    select(Study.patient_id).where(
                        Study.document_ref == "coordinator:" + user["sub"]
                    )
                ),
            )
        ]
    )
    return (
        await session.scalars(
            select(Task)
            .join(Route)
            .where(Task.assignee_role == "coordinator", *scope)
            .order_by(Task.due_at)
        )
    ).all()


@router.get("/patients/{patient_id}/protocols")
async def protocols(patient_id: UUID, user=Depends(principal), session=Depends(get_session)):
    """Полный список без limit; проверка области доступа до выдачи данных."""
    patient = await session.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(404, "Пациент не найден")
    await patient_access(user, patient_id, session)
    studies = (
        await session.scalars(
            select(Study)
            .where(Study.patient_id == patient_id)
            .order_by(Study.study_date.desc(), Study.created_at.desc(), Study.id)
        )
    ).all()
    items = []
    for study in studies:
        protocol = await session.scalar(select(Protocol).where(Protocol.study_id == study.id))
        findings = (
            await session.scalars(
                select(Finding)
                .where(Finding.study_id == study.id)
                .order_by(Finding.char_start, Finding.id)
            )
        ).all()
        matches = (
            (
                await session.scalars(
                    select(TriggerMatch).where(TriggerMatch.protocol_id == protocol.id)
                )
            ).all()
            if protocol
            else []
        )
        routes = (
            await session.scalars(
                select(Route).where(
                    Route.patient_id == patient_id,
                    Route.trigger_match_id.in_([m.id for m in matches]),
                )
            )
        ).all()
        items.append(
            {
                "study_id": study.id,
                "date": study.study_date,
                "study_type": study.study_type,
                "protocol_id": protocol.id if protocol else None,
                "findings": [
                    {
                        "finding": f.finding,
                        "quote": f.quote,
                        "char_start": f.char_start,
                        "char_end": f.char_end,
                    }
                    for f in findings
                ],
                "rules": [
                    {
                        "rule": m.applied_rule,
                        "version": m.explanation.get("version"),
                        "fired": m.fired,
                        "suppressed": m.suppressed,
                    }
                    for m in matches
                ],
                "routes": [
                    {"id": r.id, "status": r.status, "target_date": r.target_date} for r in routes
                ],
            }
        )
    return {"patient_id": patient_id, "items": items, "total": len(items)}


@router.post("/upload/preview")
async def preview(file: UploadFile = File(), study_type: str = Form(), user=Depends(principal)):
    """Обезличиваем, а не отклоняем ФИО/телефон. Ничего не сохраняем до подтверждения.

    Эвристики ограничены: оператор проверяет показанный текст целиком. Подписанный
    билет связывает подтверждение с обезличенным текстом, типом и пользователем.
    """
    if study_type not in study_types():
        raise HTTPException(422, "Выберите тип исследования из справочника")
    if not (file.filename or "").lower().endswith((".docx", ".txt")):
        raise HTTPException(422, "Поддерживаются только .docx и .txt")
    raw = await file.read(2_000_001)
    if len(raw) > 2_000_000:
        raise HTTPException(413, "Файл больше 2 МБ")
    try:
        text = _decode(raw) if raw[:2] == b"PK" else raw.decode("utf-8-sig")
    except Exception as exc:
        raise HTTPException(422, "Не удалось прочитать документ") from exc
    clean, changes = anonymize(text)
    if not clean.strip():
        raise HTTPException(422, "После обезличивания текст пуст")
    import time

    ticket = sign(
        {
            "sub": user["sub"],
            "role": user["role"],
            "exp": time.time() + 900,
            "text": clean,
            "study_type": study_type,
            "purpose": "upload",
            "study_id": str(uuid4()),
            "new_patient_id": str(uuid4()),
        }
    )
    result = _analyze(clean, study_type)
    return {
        "text": clean,
        "changes": changes,
        "ticket": ticket,
        "findings": result.findings,
        "notice": (
            "Проверьте весь текст: автоматический поиск персональных данных "
            "может пропустить необычную запись. "
            "Подтверждайте только обезличенный документ."
        ),
    }


class Confirm(BaseModel):
    ticket: str
    patient_id: UUID | None = None
    reviewed: bool


@router.post("/upload/confirm", status_code=201)
async def confirm(body: Confirm, user=Depends(principal), session=Depends(get_session)):
    """Сохраняем только подтверждённый обезличенный текст через штатное событие МИС."""
    data = verify(body.ticket)
    if data.get("purpose") != "upload" or data["sub"] != user["sub"] or not body.reviewed:
        raise HTTPException(403, "Подтвердите просмотр своего документа")
    now = get_clock().now()
    patient_id = body.patient_id
    study_id = UUID(data["study_id"])
    existing = await session.get(Study, study_id)
    if existing:
        expected = patient_id or UUID(data["new_patient_id"])
        if existing.patient_id != expected:
            raise HTTPException(409, "Этот документ уже сохранён для другого пациента")
        await patient_access(user, existing.patient_id, session)
        return {
            "patient_id": existing.patient_id,
            "study_id": existing.id,
            "result": {"duplicate": True},
        }
    if patient_id:
        if await session.get(Patient, patient_id) is None:
            raise HTTPException(404, "Пациент не найден")
        await patient_access(user, patient_id, session)
    else:
        patient_id = UUID(data["new_patient_id"])
        session.add(
            Patient(
                id=patient_id,
                external_id="anon-" + patient_id.hex[:16],
                anonymized_hash=patient_id.hex,
                created_at=now,
            )
        )
        await session.flush()
    result = await MisEventHandler().handle(
        session,
        {
            "event_id": "upload:" + str(study_id),
            "event_type": "StudyProtocolSigned",
            "occurred_at": now.isoformat(),
            "subject": {"patient_id": str(patient_id), "study_id": str(study_id)},
            "payload": {
                "text": data["text"],
                "study_type": data["study_type"],
                "study_date": now.date().isoformat(),
            },
        },
    )
    study = await session.get(Study, study_id)
    findings = (await session.scalars(select(Finding).where(Finding.study_id == study_id))).all()
    for finding in findings:
        if (
            finding.char_start is None
            or finding.char_end is None
            or data["text"][finding.char_start : finding.char_end] != finding.quote
        ):
            await session.rollback()
            raise HTTPException(422, "Извлечение вернуло некорректную цитату или смещения")
    study.document_ref = "coordinator:" + user["sub"]
    await session.commit()
    return {"patient_id": patient_id, "study_id": study_id, "result": result}
