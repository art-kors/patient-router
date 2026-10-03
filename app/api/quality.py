"""API оценки качества на размеченных исследованиях."""

from collections import defaultdict
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.models import Study
from app.services import quality
from app.services.decision.engine import DecisionEngine
from app.services.decision.matrix import load_triggers
from app.services.extraction import get_extractor
from app.settings import get_settings

router = APIRouter(prefix="/api/v1/quality", tags=["quality"])
Split = Literal["gold", "synthetic", "all"]


class MetricsOut(BaseModel):
    """Показатели выбранной выборки."""

    tp: int
    fp: int
    fn: int
    tn: int
    recall: float
    precision: float
    fpr: float
    f1: float
    n_samples: int
    split: Split


def labeled_samples() -> list[quality.LabeledSample]:
    """Загрузить разметку и привязать к реальным UUID через индекс."""
    try:
        samples = quality.load_labeled_samples(get_settings().labeled_data_dir)
    except quality.QualityDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    # резолвим demo_* -> UUID через индекс
    index = quality.load_study_index(get_settings().study_index_path)
    if index:
        try:
            samples = quality.resolve_study_ids(samples, index)
        except quality.QualityDataError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    return samples


Samples = Annotated[list[quality.LabeledSample], Depends(labeled_samples)]


def _scoped(metric, *args, **kwargs):
    """Ошибка области — это 503, а не 500: данные не дают честной метрики.

    Так эндпоинт отвечает «не смогли посчитать честно», а не отдаёт нули,
    которые читаются как «движок ничего не находит».
    """
    try:
        return metric(*args, **kwargs)
    except quality.QualityScopeError as exc:
        raise HTTPException(503, str(exc)) from exc


def _confusion(predictions, labels) -> dict:
    return _scoped(quality.confusion, predictions, labels)


def _errors(predictions, labels, limit) -> list[dict]:
    return _scoped(quality.errors, predictions, labels, limit=limit)


async def _evaluate(samples, split):
    """Прогнать реальные протоколы по текущему декодеру и матрице."""
    from app.db import SessionFactory

    grouped = defaultdict(list)
    for sample in samples:
        if split == "all" or sample.split == split:
            grouped[sample.study_id].append(sample)
    if not grouped:
        raise HTTPException(503, "Нет размеченных данных для выбранного split")
    try:
        ids = [UUID(study_id) for study_id in grouped]
    except ValueError as exc:
        raise HTTPException(
            503, "study_id разметки должен быть UUID сохранённого исследования"
        ) from exc
    try:
        async with SessionFactory() as session:
            studies = (await session.scalars(select(Study).where(Study.id.in_(ids)))).all()
    except (SQLAlchemyError, OSError) as exc:
        raise HTTPException(503, "Протоколы исследований недоступны для оценки качества") from exc
    by_id = {str(study.id): study for study in studies}
    engine = DecisionEngine()
    known = {trigger.trigger_id for trigger in engine.triggers}
    results = []
    for study_id, labels in grouped.items():
        study = by_id.get(str(UUID(study_id)))
        if study is None or not study.raw_text or not study.raw_text.strip():
            raise HTTPException(503, f"Нет протокола для размеченного исследования {study_id}")
        if any(sample.trigger_id not in known for sample in labels):
            raise HTTPException(503, "Разметка содержит триггер, отсутствующий в текущей матрице")
        extraction = get_extractor().extract(study.raw_text, study_type=study.study_type)
        decision = engine.decide(
            extraction.findings,
            study_type=extraction.meta.study_type,
            conclusion_text=extraction.conclusion_text,
        )
        # Обёртка фиксирует исследование: метрика считается строго по этому
        # study_id, и предсказания не могут молча уехать в чужую оценку.
        results.append((quality.StudyPredictions(study_id, tuple(decision.matches)), labels))
    return results


@router.get("/metrics", response_model=MetricsOut)
async def metrics(samples: Samples, split: Split = "all") -> MetricsOut:
    """Метрики по всем исследованиям выбранной выборки.

    Считаются по одному исследованию за раз, затем складываются: одна оценка,
    смешанная из разных протоколов, дала бы неправдоподобные числа.
    """
    counts = dict.fromkeys(("tp", "fp", "fn", "tn"), 0)
    for predictions, labels in await _evaluate(samples, split):
        for key, value in _confusion(predictions, labels).items():
            counts[key] += value
    result = quality.QualityMetrics(**counts)
    return MetricsOut(
        **{key: getattr(result, key) for key in MetricsOut.model_fields if key != "split"},
        split=split,
    )


@router.get("/confusion")
async def confusion(samples: Samples, split: Split = "all") -> dict:
    """Матрица TP, FP, FN, TN."""
    result = await metrics(samples, split)
    return result.model_dump(include={"tp", "fp", "fn", "tn"})


@router.get("/errors")
async def errors(
    samples: Samples, split: Split = "all", limit: Annotated[int, Query(ge=0)] = 50
) -> list[dict]:
    """Расхождения с цитатами, ограниченные общим лимитом."""
    result = []
    for predictions, labels in await _evaluate(samples, split):
        result.extend(_errors(predictions, labels, limit - len(result)))
    return result


@router.get("/coverage")
def coverage(samples: Samples) -> dict:
    """Покрытие триггеров разметкой без обращения к БД."""
    return quality.coverage(load_triggers(), samples)
