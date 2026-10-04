"""API оценки качества на размеченных исследованиях."""

from collections import defaultdict
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
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


class CoverageOut(BaseModel):
    """Покрытие триггеров разметкой и честный отказ, когда разметки нет.

    Поля ``covered``/``ratio`` nullable не для удобства: без разметки покрытие
    посчитать нельзя, и null честнее нуля — ноль читался бы как «триггеры не
    проверялись движком», то есть как правдивый, но неверный вывод.
    """

    total: int = Field(description="Всего триггеров в матрице — считается всегда")
    covered: int | None = Field(description="Триггеров с примерами в разметке; null без разметки")
    uncovered: list[str] = Field(description="Триггеры без примеров в разметке")
    ratio: float | None = Field(description="Доля покрытия; null без разметки")
    labeled_samples: int = Field(description="Сколько записей разметки прочитано")
    available: bool = Field(description="Есть ли данные для оценки покрытия")
    message: str | None = Field(
        default=None, description="Почему покрытие не посчитано, когда available=false"
    )


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


def optional_samples() -> list[quality.LabeledSample] | None:
    """Разметка для ручек, которые умеют ответить и без неё.

    Отсутствие или порча разметки — не 503, а ``None``: решение о том, что с
    этим делать, принимает сама ручка. ``/coverage`` отвечает «нет данных для
    оценки покрытия», а ``/metrics`` по-прежнему отказывает: без разметки там
    нечего считать вовсе.
    """
    try:
        return labeled_samples()
    except HTTPException:
        return None


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
    # Отключённое правило остаётся известным разметке: отсутствие
    # срабатывания считается FN/TN, а не повреждением данных.
    known = {trigger.trigger_id for trigger in _quality_triggers()}
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
    samples: Samples,
    split: Split = "all",
    limit: Annotated[int, Query(ge=0)] = 50,
    trigger_id: str | None = None,
) -> list[dict]:
    """Расхождения с цитатами, ограниченные общим лимитом."""
    result = []
    for predictions, labels in await _evaluate(samples, split):
        if trigger_id is not None:
            labels = [sample for sample in labels if sample.trigger_id == trigger_id]
        result.extend(_errors(predictions, labels, limit - len(result)))
    return result


@router.get("/coverage", response_model=CoverageOut)
def coverage(
    samples: Annotated[list[quality.LabeledSample] | None, Depends(optional_samples)],
) -> CoverageOut:
    """Покрытие триггеров разметкой без обращения к БД.

    Разметка хакатона не публикуется, поэтому в свежем клоне её нет — и раньше
    ручка отдавала 503, хотя README обещал рабочий ответ. Теперь общее число
    триггеров считается всегда (оно лежит в ``config/routing_matrix.json``),
    а покрытие требует разметки и без неё честно сообщает, что данных для
    оценки нет: ``covered`` и ``ratio`` равны null, а не 0.
    """
    triggers = load_triggers()
    total = len({trigger.trigger_id for trigger in triggers})
    if not samples:
        return CoverageOut(
            total=total,
            covered=None,
            uncovered=[],
            ratio=None,
            labeled_samples=0,
            available=False,
            message=(
                "Нет данных для оценки покрытия: каталог разметки "
                f"{get_settings().labeled_data_dir} пуст или отсутствует. "
                "Матрица содержит "
                f"{total} триггер(ов) — проверьте их через POST /api/v1/analyze."
            ),
        )
    result = quality.coverage(triggers, samples)
    return CoverageOut(**result, labeled_samples=len(samples), available=True)


def _quality_triggers():
    """Для оценки нужны также отключённые правила с сохранённой разметкой."""
    from app.services.decision.matrix_store import database_triggers

    stored = database_triggers(include_disabled=True)
    return stored if stored is not None else load_triggers()


@router.get("/metrics/by-trigger")
async def metrics_by_trigger(samples: Samples, split: Split = "all") -> list[dict]:
    """Метрики каждого правила; available=false означает отсутствие разметки."""
    evaluated = await _evaluate(samples, split)
    result = []
    active_ids = {trigger.trigger_id for trigger in load_triggers()}
    for trigger in _quality_triggers():
        counts = dict.fromkeys(("tp", "fp", "fn", "tn"), 0)
        for predictions, labels in evaluated:
            selected = [label for label in labels if label.trigger_id == trigger.trigger_id]
            for key, value in _confusion(predictions, selected).items():
                counts[key] += value
        metric = quality.QualityMetrics(**counts)
        result.append(
            {
                "trigger_id": trigger.trigger_id,
                "display_name": trigger.display_name,
                **{key: getattr(metric, key) for key in MetricsOut.model_fields if key != "split"},
                "split": split,
                "available": metric.n_samples > 0,
                "enabled": trigger.trigger_id in active_ids,
            }
        )
    return result


@router.get("/metrics/timeline")
async def metrics_timeline(samples: Samples, split: Split = "all") -> dict:
    """Зафиксировать текущую оценку и вернуть историю снимков по датам.

    Исторические снимки не пересчитываются новым декодером. Дата снимка —
    дата оценки, а не дата исследования. Без успешной оценки снимок не пишется.
    """
    from datetime import UTC, datetime

    from sqlalchemy import insert

    from app.db import SessionFactory
    from app.services.decision.matrix_store import MatrixStore, snapshots

    try:
        async with SessionFactory() as session:
            store = MatrixStore(session)
            await store.prepare()
            # Блокировка конфигурации связывает оценку с точной версией.
            history = await store.history()
            current = await metrics(samples, split)
            await session.execute(
                insert(snapshots).values(
                    created_at=datetime.now(UTC),
                    split=split,
                    matrix_version=history[0]["version"] if history else 1,
                    decoder=get_extractor().name,
                    metrics=current.model_dump(),
                )
            )
            await session.commit()
            rows = await session.execute(
                select(snapshots)
                .where(snapshots.c.split == split)
                .order_by(snapshots.c.created_at.desc())
                .limit(100)
            )
            return {
                "points": [dict(row) for row in reversed(list(rows.mappings()))],
                "message": "Снимки оценок по времени; сохраняются при обновлении дашборда",
            }
    except (SQLAlchemyError, OSError) as exc:
        raise HTTPException(503, "История метрик недоступна: нет соединения с БД") from exc
