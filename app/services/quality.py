"""Метрики распознавания и загрузка врачебной разметки."""

import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

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


class QualityScopeError(ValueError):
    """Предсказания и разметка относятся к разным исследованиям.

    Отдельный класс, а не QualityDataError: ошибка не в файлах разметки,
    а в том, как вызывающий код собрал их в одну оценку. Выдуманные числа
    хуже явного отказа, поэтому считать метрику в таком случае нельзя.
    """


@dataclass(frozen=True)
class StudyPredictions:
    """Предсказания одного исследования с явным study_id.

    TriggerMatch протокола не знает — движок принимает находки, а не запись
    из БД. Обёртка восстанавливает принадлежность к исследованию, чтобы
    сопоставление шло по паре (study_id, trigger_id), а не по одному trigger_id.
    """

    study_id: str
    matches: tuple[TriggerMatch, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.study_id, str) or not self.study_id.strip():
            raise ValueError("study_id предсказаний должен быть непустой строкой")


def _single_study(samples: list[LabeledSample]) -> str:
    """study_id всех образцов; пусто — образцов нет; >1 — понятная ошибка."""
    study_ids = {sample.study_id for sample in samples}
    if len(study_ids) > 1:
        listed = ", ".join(sorted(study_ids))
        raise QualityScopeError(
            "Одна оценка — одно исследование, а разметка относится к нескольким: "
            f"{listed}. Сгруппируйте образцы по study_id и сложите матрицы."
        )
    return next(iter(study_ids), "")


def _as_study(predictions, samples: list[LabeledSample]) -> tuple[str, tuple[TriggerMatch, ...]]:
    """Свести предсказания к паре (study_id, матчи).

    StudyPredictions проверяем: он несёт исследование, и мы убеждаемся, что
    разметка — про него же. Список TriggerMatch допускаем для удобства вызовов,
    но тогда исследование выводится только из разметки, и смешивание
    исследований отсекается ошибкой, а не «всё не сработало».
    """
    if isinstance(predictions, StudyPredictions):
        sample_study = _single_study(samples)
        if sample_study and sample_study != predictions.study_id:
            raise QualityScopeError(
                f"Предсказания получены для исследования {predictions.study_id}, "
                f"а разметка — для {sample_study}. Смешивать их нельзя."
            )
        return predictions.study_id, predictions.matches
    return _single_study(samples), tuple(predictions)


def _pairs(predictions, samples: list[LabeledSample], split: str | None):
    """Сопоставить предсказания с разметкой по паре (исследование, триггер).

    Отсутствие предсказания по триггеру — несрабатывание. Предсказание чужого
    исследования или два результата по одному триггеру — ошибка: иначе тихо
    получились бы бессмысленные метрики.

    Границы оценки задаёт split: проверка области идёт по его выборке, поэтому
    можно передать всю разметку и попросить один срез.
    """
    selected = [s for s in samples if split is None or s.split == split]
    study_id, predictions = _as_study(predictions, selected)
    if not study_id and predictions:
        raise QualityScopeError(
            "Нет разметки, по которой можно подтвердить исследование предсказаний. "
            "Передайте StudyPredictions(study_id, ...) или образцы одного исследования."
        )
    matches: dict[tuple[str, str], TriggerMatch] = {}
    for match in predictions:
        key = (study_id, match.trigger.trigger_id)
        if key in matches:
            raise QualityScopeError(
                f"Два предсказания по триггеру {key[1]} для исследования {key[0]}: "
                "одно исследование даёт один результат по триггеру."
            )
        matches[key] = match
    yield from ((sample, matches.get((sample.study_id, sample.trigger_id))) for sample in selected)


def compute_metrics(
    predictions: StudyPredictions | list[TriggerMatch],
    samples: list[LabeledSample],
    split: str | None = None,
) -> QualityMetrics:
    """Посчитать матрицу для выбранной части разметки."""
    counts = dict.fromkeys(("tp", "fp", "fn", "tn"), 0)
    for sample, match in _pairs(predictions, samples, split):
        fired = match is not None and match.fired
        key = ("tp" if sample.label else "fp") if fired else ("fn" if sample.label else "tn")
        counts[key] += 1
    return QualityMetrics(**counts)


def confusion(
    predictions: StudyPredictions | list[TriggerMatch],
    samples: list[LabeledSample],
    split: str | None = None,
) -> dict:
    """Вернуть матрицу словарём."""
    metrics = compute_metrics(predictions, samples, split)
    return {key: getattr(metrics, key) for key in ("tp", "fp", "fn", "tn")}


def errors(
    predictions: StudyPredictions | list[TriggerMatch],
    samples: list[LabeledSample],
    split: str | None = None,
    limit: int = 50,
) -> list[dict]:
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


def load_study_index(path: str | Path) -> dict[str, str]:
    """Загрузить маппинг demo_* study_id -> UUID. Отсутствие файла — пустой маппинг."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise QualityDataError(f"{p}: ожидается объект {{demo_id: uuid}}")
        return {str(k): str(v) for k, v in data.items()}
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise QualityDataError(f"Индекс исследований недоступен: {exc}") from exc


def resolve_study_ids(samples: list[LabeledSample], index: dict[str, str]) -> list[LabeledSample]:
    """Заменить demo_* идентификаторы на UUID через индекс. Валидные UUID оставляем как есть."""
    resolved = []
    for s in samples:
        sid = s.study_id
        try:
            UUID(sid)  # уже UUID — оставляем
            resolved.append(s)
        except ValueError:
            uuid = index.get(sid)
            if not uuid:
                raise QualityDataError(
                    f"study_id {sid!r} не найден в индексе {list(index)[:3]}..."
                ) from None
            resolved.append(
                LabeledSample(
                    study_id=uuid,
                    trigger_id=s.trigger_id,
                    label=s.label,
                    split=s.split,
                )
            )
    return resolved


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
