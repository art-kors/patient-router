"""Метрики распознавания и загрузка врачебной разметки."""

import json
from dataclasses import dataclass
from pathlib import Path

from app.services.decision.engine import TriggerMatch
from app.services.decision.matrix import TriggerDef


@dataclass(frozen=True)
class LabeledSample:
    """Ожидаемое решение для триггера одного исследования."""

    study_id: str
    trigger_id: str
    label: bool
    split: str

    def __post_init__(self) -> None:
        if not isinstance(self.study_id, str) or not self.study_id.strip():
            raise ValueError("study_id должен быть непустой строкой")
        if not isinstance(self.trigger_id, str) or not self.trigger_id.strip():
            raise ValueError("trigger_id должен быть непустой строкой")
        if type(self.label) is not bool or self.split not in ("gold", "synthetic"):
            raise ValueError("label должен быть bool, split — gold или synthetic")


@dataclass
class QualityMetrics:
    """Матрица ошибок и вычисляемые показатели; пустой знаменатель даёт ноль."""

    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def fpr(self) -> float:
        """Доля ложных срабатываний на нормах; целевое значение ≤ 0.05."""
        return self.fp / (self.fp + self.tn) if self.fp + self.tn else 0.0

    @property
    def f1(self) -> float:
        denominator = 2 * self.tp + self.fp + self.fn
        return 2 * self.tp / denominator if denominator else 0.0

    @property
    def n_samples(self) -> int:
        return self.tp + self.fp + self.fn + self.tn


def _pairs(predictions, samples, split):
    """Предсказания относятся к одному исследованию; отсутствие — несрабатывание."""
    matches = {match.trigger.trigger_id: match for match in predictions}
    for sample in samples:
        if split is None or sample.split == split:
            yield sample, matches.get(sample.trigger_id)


def compute_metrics(
    predictions: list[TriggerMatch], samples: list[LabeledSample], split: str | None = None
) -> QualityMetrics:
    """Посчитать матрицу для выбранной части разметки."""
    counts = dict.fromkeys(("tp", "fp", "fn", "tn"), 0)
    for sample, match in _pairs(predictions, samples, split):
        fired = match is not None and match.fired
        key = ("tp" if sample.label else "fp") if fired else ("fn" if sample.label else "tn")
        counts[key] += 1
    return QualityMetrics(**counts)


def confusion(predictions, samples, split=None) -> dict:
    """Вернуть матрицу словарём."""
    metrics = compute_metrics(predictions, samples, split)
    return {key: getattr(metrics, key) for key in ("tp", "fp", "fn", "tn")}


def errors(predictions, samples, split=None, limit=50) -> list[dict]:
    """Расхождения FP и FN с цитатой и правилом для проверки врачом."""
    result = []
    for sample, match in _pairs(predictions, samples, split):
        fired = match is not None and match.fired
        if fired == sample.label:
            continue
        if len(result) >= max(0, limit):
            break
        result.append(
            {
                "trigger_id": sample.trigger_id,
                "study_id": sample.study_id,
                "type": "fp" if fired else "fn",
                "label": sample.label,
                "fired": fired,
                "quote": match.quote if match else "",
                "applied_rule": match.applied_rule if match else "",
                "suppression_reason": match.suppression_reason if match else "no_match",
            }
        )
    return result


def coverage(triggers: list[TriggerDef], samples: list[LabeledSample]) -> dict:
    """Покрытие матрицы примерами, включая нормы."""
    ids = list(dict.fromkeys(trigger.trigger_id for trigger in triggers))
    labeled = {sample.trigger_id for sample in samples}
    uncovered = [trigger_id for trigger_id in ids if trigger_id not in labeled]
    covered = len(ids) - len(uncovered)
    return {
        "total": len(ids),
        "covered": covered,
        "uncovered": uncovered,
        "ratio": covered / len(ids) if ids else 0.0,
    }


class QualityDataError(ValueError):
    """Данные отсутствуют или непригодны для честной оценки."""


def load_labeled_samples(directory: str | Path) -> list[LabeledSample]:
    """Загрузить списки разметки из всех JSON-файлов указанного каталога."""
    try:
        files = sorted(Path(directory).glob("*.json"))
        if not files:
            raise QualityDataError(f"Нет размеченных данных: ожидаются {directory}/*.json")
        samples = []
        seen = set()
        for path in files:
            items = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(items, list):
                raise QualityDataError(f"{path}: ожидается список объектов разметки")
            for item in items:
                sample = LabeledSample(**item)
                key = (sample.study_id, sample.trigger_id)
                if key in seen:
                    raise QualityDataError(f"{path}: повторная разметка {key}")
                seen.add(key)
                samples.append(sample)
        if not samples:
            raise QualityDataError("Нет размеченных данных: файлы разметки пусты")
        return samples
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise QualityDataError(f"Разметка недоступна: {exc}") from exc
