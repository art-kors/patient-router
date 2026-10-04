"""Аналитика замкнутого цикла: разбор → врач → повторная оценка правил."""

from types import SimpleNamespace
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from app.api.analysis import _analyze
from app.api.quality import optional_samples
from app.clock import get_clock
from app.db import get_session
from app.models import AnalysisFeedback, AnalysisRun, Study
from app.services.analytics import aggregate, digest, safe_findings, safe_matches
from app.services.decision.engine import DecisionEngine

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])


async def session_dependency(session=Depends(get_session)):
    """Недоступность хранилища объясняется русским сообщением."""
    try:
        yield session
    except (SQLAlchemyError, OSError) as exc:
        raise HTTPException(503, "Хранилище аналитики недоступно") from exc


Session = Annotated[object, Depends(session_dependency)]


class FeedbackInput(BaseModel):
    """Подтверждение, ложная или пропущенная находка."""

    label: Literal["confirmed", "false_positive", "missed"]


@router.put("/analyses/{analysis_id}/feedback/{trigger_id}")
async def feedback(analysis_id: UUID, trigger_id: str, body: FeedbackInput, session: Session):
    """Заменить оценку пары атомарно: повтор запроса не добавляет записи."""
    run = await session.get(AnalysisRun, analysis_id)
    if run is None:
        raise HTTPException(404, "Разбор не найден")
    match = next((m for m in run.matches if m["trigger_id"] == trigger_id), None)
    if match is None:
        raise HTTPException(422, "Триггер отсутствует в редакции правил этого разбора")
    if (body.label in ("confirmed", "false_positive")) != bool(match["fired"]):
        raise HTTPException(
            422, "Подтверждать и отклонять можно сработавшую находку; пропуск — несработавшую"
        )
    now = get_clock().now()
    stmt = (
        insert(AnalysisFeedback)
        .values(
            id=uuid4(),
            analysis_id=analysis_id,
            trigger_id=trigger_id,
            label=body.label,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[AnalysisFeedback.analysis_id, AnalysisFeedback.trigger_id],
            set_={"label": body.label, "updated_at": now},
            where=AnalysisFeedback.label != body.label,
        )
    )
    await session.execute(stmt)
    await session.commit()
    return {"analysis_id": str(analysis_id), "trigger_id": trigger_id, "label": body.label}


@router.get("/analyses")
async def analyses(session: Session, limit: Annotated[int, Query(ge=1, le=200)] = 50):
    """Последние разборы без текста протоколов и цитат."""
    runs = (
        await session.scalars(
            select(AnalysisRun)
            .order_by(AnalysisRun.created_at.desc(), AnalysisRun.id.desc())
            .limit(limit)
        )
    ).all()
    ids = [r.id for r in runs]
    feedback = (
        await session.scalars(select(AnalysisFeedback).where(AnalysisFeedback.analysis_id.in_(ids)))
    ).all()
    labels = {(f.analysis_id, f.trigger_id): f.label for f in feedback}
    return [
        {
            "id": str(r.id),
            "created_at": r.created_at.isoformat(),
            "study_type": r.study_type,
            "decoder_used": r.decoder_used,
            "fallback": r.fallback,
            "duration_ms": float(r.duration_ms),
            "text_length": r.text_length,
            "matches": [dict(m, feedback=labels.get((r.id, m["trigger_id"]))) for m in r.matches],
        }
        for r in runs
    ]


@router.get("/metrics")
async def report(session: Session, current: bool = False):
    """Объединить доступную разметку и врача; неразмеченные разборы не оцениваются."""
    runs = (await session.scalars(select(AnalysisRun))).all()
    feedback = (await session.scalars(select(AnalysisFeedback))).all()
    samples = optional_samples() or []
    # Разметка привязывается по хэшу текста существующего исследования.
    # Один протокол оценивается один раз: выбираем последний его разбор.
    latest = {}
    for run in sorted(runs, key=lambda r: (r.created_at, str(r.id))):
        latest[run.text_hash] = run
    generated = {}
    ids = []
    for sample in samples:
        try:
            ids.append(UUID(sample.study_id))
        except ValueError:
            continue
    studies = (await session.scalars(select(Study).where(Study.id.in_(ids)))).all() if ids else []
    hashes = {str(s.id): digest(s.raw_text) for s in studies if s.raw_text}
    source_runs = []
    for study in studies:
        if not study.raw_text or digest(study.raw_text) in latest:
            continue
        response = _analyze(study.raw_text, study.study_type)
        run = SimpleNamespace(
            id="source:" + str(study.id),
            text_hash=digest(study.raw_text),
            study_type=response.study_type,
            decoder_used=response.decoder_used,
            findings=safe_findings(response),
            matches=safe_matches(response),
            stored=False,
        )
        source_runs.append(run)
        latest[run.text_hash] = run
    # Врачебный gold имеет приоритет перед синтетикой ещё до ручных отметок.
    for sample in sorted(samples, key=lambda s: s.split == "gold"):
        run = latest.get(hashes.get(sample.study_id))
        if run:
            generated[(str(run.id), sample.trigger_id)] = sample.label
    engine = DecisionEngine()
    result = aggregate(
        runs + source_runs, feedback, current=current, engine=engine, generated=generated
    )
    result["analyses"] = len(runs)
    result["generated_studies"] = len(source_runs)
    result["generated_labels"] = len(generated)
    result["unlinked_labels"] = sum(sample.study_id not in hashes for sample in samples)
    result["doctor_metrics"] = aggregate(runs, feedback, current=current, engine=DecisionEngine())[
        "metrics"
    ]
    result["mode"] = "current_structured" if current else "recorded"
    result["message"] = (
        "Переоценка структурированных признаков; для новых синонимов "
        "и текстовых порогов повторите разбор протокола."
        if current
        else "Исторические решения; врачебная разметка важнее сгенерированной."
    )
    return result
