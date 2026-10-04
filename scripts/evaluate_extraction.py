"""Воспроизводимая оценка декодеров на синтетических демо-протоколах."""

import re
from collections import defaultdict
from pathlib import Path

from app.services.decision import DecisionEngine
from app.services.extraction import DictionaryExtractor
from app.services.quality import (
    LabeledSample,
    QualityMetrics,
    StudyPredictions,
    compute_metrics,
    load_labeled_samples,
)
from doc_processing.adapter import TeammateExtractor
from scripts.make_demo_data import GOLD, STUDY_GROUPS, build_labels, load_matrix, natural_key


def demo_labels() -> list[LabeledSample]:
    """Восстановить опубликованную разметку той же функцией, что генератор демо."""
    fragments = {
        "gynecology": "1 Ж ОМТ",
        "abdomen": "1 ЖП",
        "breast": "молочн железа",
        "thyroid": "щитовидка",
        "lower_limb": "ниж",
    }
    studies = []
    source_of = {}
    for prefix, fragment in fragments.items():
        names = sorted((Path(name) for name in GOLD if name.startswith(fragment)), key=natural_key)
        source_study = next(info[1] for info in STUDY_GROUPS.values() if info[0] == prefix)
        for index, name in enumerate(names, 1):
            study_id = f"demo_{prefix}_{index:02d}"
            studies.append((study_id, source_study))
            source_of[study_id] = name.name
    return [LabeledSample(**item) for item in build_labels(studies, load_matrix(), source_of)]


def main() -> None:
    """Прогнать все протоколы; складывать матрицы строго по исследованиям."""
    labels_path = Path("data/labeled")
    if list(labels_path.glob("*.json")):
        samples = load_labeled_samples(labels_path)
        print("Разметка: data/labeled/*.json")
    else:
        samples = demo_labels()
        print("Разметка восстановлена из GOLD генератора демо (исходный файл отсутствует)")
    grouped = defaultdict(list)
    for sample in samples:
        grouped[sample.study_id].append(sample)
    paths = sorted(Path("data/demo/protocols").glob("*.txt"))
    if not paths or set(grouped) - {path.stem for path in paths}:
        raise ValueError("Нет демо-протоколов для оценки всей разметки")
    engine = DecisionEngine()
    print(f"Протоколов: {len(paths)}; размеченных пар: {len(samples)}")
    for extractor in (DictionaryExtractor(), TeammateExtractor()):
        counts = dict.fromkeys(("tp", "fp", "fn", "tn"), 0)
        for path in paths:
            text = path.read_text(encoding="utf-8")
            match = re.search(r"# тип исследования: (.*)", text)
            study_type = match.group(1) if match else None
            extraction = extractor.extract(text, study_type=study_type)
            decision = engine.decide(
                extraction.findings,
                study_type=study_type,
                conclusion_text=extraction.conclusion_text,
            )
            metric = compute_metrics(
                StudyPredictions(path.stem, tuple(decision.matches)), grouped[path.stem]
            )
            for key in counts:
                counts[key] += getattr(metric, key)
        metric = QualityMetrics(**counts)
        print(
            f"{extractor.name}: {counts}; recall={metric.recall:.3f}; "
            f"precision={metric.precision:.3f}; FPR={metric.fpr:.3f}"
        )


if __name__ == "__main__":
    main()
