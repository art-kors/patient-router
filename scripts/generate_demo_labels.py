"""Перегенерация демо-разметки из GOLD без доступа к исходным DOCX."""

import hashlib
import json
from pathlib import Path

from app.services.quality import LabeledSample
from scripts.make_demo_data import GOLD, REPO_ROOT, STUDY_GROUPS, build_labels, natural_key

LABELS_DIR = REPO_ROOT / "data/labeled"
PROTOCOLS_DIR = REPO_ROOT / "data/demo/protocols"
MANIFEST_PATH = LABELS_DIR / "protocols.manifest"


def generate_labels() -> list[dict]:
    """Воспроизвести исходные категории независимо от новой матрицы врача."""
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
    matrix = json.loads((REPO_ROOT / "config/routing_matrix_legacy.json").read_text())
    return build_labels(studies, matrix, source_of)


def serialize_labels() -> bytes:
    """Фиксировать порядок записей, кодировку и завершающий перевод строки."""
    return (json.dumps(generate_labels(), ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def verify_protocol_coverage(samples: list[LabeledSample], directory: Path) -> None:
    """Проверить все файлы, их хеши и пары; исключения указаны явно в манифесте."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    paths = {path.stem: path for path in directory.glob("*.txt")}
    if set(paths) != set(manifest):
        raise ValueError("Рассинхрон: состав протоколов отличается от манифеста разметки")
    expected_pairs = set()
    for study_id, entry in manifest.items():
        if hashlib.sha256(paths[study_id].read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError(f"Рассинхрон: изменён протокол {study_id}")
        expected_pairs.update((study_id, trigger_id) for trigger_id in entry["trigger_ids"])
        if (
            not entry["trigger_ids"]
            and entry.get("excluded_reason") != "Нет категории в исходной матрице"
        ):
            raise ValueError(f"Нет разметки или причины исключения: {study_id}")
    actual_pairs = [(sample.study_id, sample.trigger_id) for sample in samples]
    if set(actual_pairs) != expected_pairs or len(actual_pairs) != len(expected_pairs):
        raise ValueError("Рассинхрон: пары разметки не соответствуют протоколам")


def main() -> None:
    """Записать разметку; манифест меняется только после отдельной проверки текстов."""
    LABELS_DIR.mkdir(parents=True, exist_ok=True)
    (LABELS_DIR / "labeled.json").write_bytes(serialize_labels())


if __name__ == "__main__":
    main()
