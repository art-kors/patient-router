"""Воспроизводимая оценка декодеров на синтетических демо-протоколах."""

import argparse
import re
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

from app.services.decision import DecisionEngine
from app.services.decision.matrix import load_triggers
from app.services.extraction import DictionaryExtractor
from app.services.quality import (
    QualityMetrics,
    StudyPredictions,
    compute_metrics,
    load_labeled_samples,
)
from doc_processing.adapter import TeammateExtractor
from scripts.generate_demo_labels import verify_protocol_coverage
from scripts.import_clinical_matrix import OUTPUT_PATH


def main() -> None:
    """Прогнать все протоколы; складывать матрицы строго по исследованиям."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args()
    samples = load_labeled_samples(Path("data/labeled"))
    print("Разметка: data/labeled/labeled.json (файл репозитория)")
    verify_protocol_coverage(samples, Path("data/demo/protocols"))
    grouped = defaultdict(list)
    for sample in samples:
        grouped[sample.study_id].append(sample)
    paths = sorted(Path("data/demo/protocols").glob("*.txt"))
    if not paths or set(grouped) - {path.stem for path in paths}:
        raise ValueError("Нет демо-протоколов для оценки всей разметки")
    engine = DecisionEngine(load_triggers(args.matrix))
    print(f"Протоколов: {len(paths)}; размеченных пар: {len(samples)}")
    teammate = TeammateExtractor()
    teammate.dictionary = DictionaryExtractor(args.matrix)
    teammate._negatives = teammate.dictionary._negatives
    print("Оценка по исходным категориям; новые сценарии без разметки исключены")
    for extractor in (DictionaryExtractor(args.matrix), teammate):
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
            # Проекция только явных объединений на исходные категории разметки.
            # Новые сценарии без разметки не считаем ни нормой, ни ошибкой.
            projected = []
            for match in decision.matches:
                ids = match.trigger.legacy_trigger_ids or (match.trigger.trigger_id,)
                for identifier in ids:
                    projected.append(
                        replace(match, trigger=replace(match.trigger, trigger_id=identifier))
                    )
            metric = compute_metrics(
                StudyPredictions(path.stem, tuple(projected)), grouped[path.stem]
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
