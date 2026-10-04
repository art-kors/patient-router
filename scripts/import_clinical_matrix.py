"""Воспроизводимый импорт врачебной матрицы с сохранением происхождения правил."""

import argparse
import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / "data/clinical/routing_matrix_clinician.csv"
LEGACY_PATH = ROOT / "config/routing_matrix_legacy.json"
OUTPUT_PATH = ROOT / "config/routing_matrix.json"
# Только явно рассмотренные пересечения; номер строки не участвует в идентификаторе.
MERGES = {
    "Миома матки": "fibroid_uterus",
    "Подозрение на полип эндометрия и полип шейки матки (Показание к операции)": (
        "endometrial_polyp"
    ),
    (
        "Признаки гемодинамически значимого атеросклероза артерий нижних конечностей. "
        "Окклюзия левой ЗББА"
    ): "lower_limb_stenosis",
}
TRANSLIT = dict(
    zip(
        "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
        (
            "a",
            "b",
            "v",
            "g",
            "d",
            "e",
            "yo",
            "zh",
            "z",
            "i",
            "y",
            "k",
            "l",
            "m",
            "n",
            "o",
            "p",
            "r",
            "s",
            "t",
            "u",
            "f",
            "kh",
            "ts",
            "ch",
            "sh",
            "shch",
            "",
            "y",
            "",
            "e",
            "yu",
            "ya",
        ),
        strict=True,
    )
)


def identifier(name: str) -> str:
    """Читаемый идентификатор из названия; никакого хеша и случайности."""
    result = re.sub(r"[^a-z0-9]+", "_", "".join(TRANSLIT.get(c, c) for c in name.lower())).strip(
        "_"
    )
    if not result:
        raise ValueError("Название не содержит символов для идентификатора")
    return result


def study_name(value: str) -> str:
    """Свести варианты названий к типам исследований, различая артерии и вены."""
    lowered = value.lower()
    if "малого таза" in lowered:
        return "УЗИ органов малого таза"
    if "брюшной полости" in lowered or "гепатобилиарной" in lowered:
        return "УЗИ брюшной полости"
    if "нижних конечностей" in lowered:
        if "артерий и вен" in lowered:
            return "УЗДС артерий и вен нижних конечностей"
        if "артерий" in lowered:
            return "УЗДГ артерий нижних конечностей"
        if "вен" in lowered:
            return "УЗДС вен нижних конечностей"
    raise ValueError(f"Неизвестный тип исследования: {value}")


def split_list(value: str, separator: str = ",") -> list[str]:
    """Разобрать внутренний список, сохранив порядок и все непустые элементы."""
    parts = next(csv.reader([value], delimiter=separator, skipinitialspace=True))
    return [
        part.strip().strip('"«»').strip() for part in parts if part.strip().strip('"«»').strip()
    ]


def formal_thresholds(value: str) -> dict:
    """Преобразовать только однозначную числовую границу без альтернатив и условий."""
    from app.services.decision.threshold_text import parse_threshold_text

    return parse_threshold_text(value).thresholds


def convert(csv_path: Path = CSV_PATH, legacy_path: Path = LEGACY_PATH) -> list[dict]:
    """Врач задаёт маршруты; прежние правила дополняют отсутствующие сценарии."""
    legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
    by_id = {item["trigger_id"]: item for item in legacy}
    result = []
    merged = set()
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = {
            "display_name": "display_name (Находка)",
            "source_study": "source_study (Где искать)",
            "synonyms": "synonyms (Синонимы в тексте)",
            "negative_contexts": "negative_contexts (Отрицания/Норма)",
            "required_attributes": "required_attributes (Атрибуты)",
            "threshold_text": "thresholds (Пороги)",
            "specialty": "specialty (Специалист)",
            "potential_route": "potential_route (Возможный маршрут)",
            "target_sla_days": "target_sla_days (SLA дни)",
            "priority": "priority (Приоритет)",
            "emergency_flag": "emergency_flag (Экстренность)",
            "evidence_phrases": "Фраза-доказательство",
        }
        department = next(
            (
                name
                for name in ("department (Подразделение)", "department (Подделение)")
                if name in (reader.fieldnames or [])
            ),
            None,
        )
        missing = set(fields.values()) - set(reader.fieldnames or [])
        if missing or department is None:
            raise ValueError(
                f"В CSV отсутствуют обязательные колонки: {sorted(missing)}; "
                f"подразделение: {department}"
            )
        for line, row in enumerate(reader, 2):
            item = {key: row[column].strip() for key, column in fields.items()}
            item["department"] = row[department].strip()
            name = item["display_name"]
            old_id = MERGES.get(name)
            item["trigger_id"] = identifier(name)
            item["source_study"] = study_name(item["source_study"])
            for key in ("synonyms", "negative_contexts", "required_attributes"):
                item[key] = split_list(item[key])
            item["evidence_phrases"] = split_list(item["evidence_phrases"], ";")
            # Название нужно для сопоставления нормализованной находки словарного декодера.
            item["synonyms"] = list(dict.fromkeys([name, *item["synonyms"]]))
            item["thresholds"] = formal_thresholds(item["threshold_text"])
            for key in ("priority", "target_sla_days"):
                try:
                    item[key] = int(item[key])
                except ValueError as exc:
                    raise ValueError(f"Строка {line}: {key} должен быть целым числом") from exc
            if item["emergency_flag"].upper() not in ("TRUE", "FALSE"):
                raise ValueError(f"Строка {line}: экстренность должна быть TRUE или FALSE")
            item["emergency_flag"] = item["emergency_flag"].upper() == "TRUE"
            item.update(
                version=2, provenance="clinician", legacy_trigger_ids=[], clinical_source=dict(row)
            )
            if old_id:
                old = by_id[old_id]
                merged.add(old_id)
                item["legacy_trigger_ids"] = [old_id]
                # Сохраняем отрицания обеих редакций, но числовые догадки не переносим.
                item["negative_contexts"] = list(
                    dict.fromkeys([*item["negative_contexts"], *old["negative_contexts"]])
                )
                item["synonyms"] = list(dict.fromkeys([*item["synonyms"], *old["synonyms"]]))
            result.append(item)
    for old in legacy:
        if old["trigger_id"] not in merged:
            result.append(dict(old, provenance="project", legacy_trigger_ids=[old["trigger_id"]]))
    ids = [item["trigger_id"] for item in result]
    if len(ids) != len(set(ids)):
        raise ValueError("После транслитерации обнаружены повторяющиеся идентификаторы")
    return result


def main() -> None:
    """Сохранить JSON в фиксированном порядке, без времени и случайных значений."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=CSV_PATH)
    parser.add_argument("--legacy", type=Path, default=LEGACY_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args()
    from app.services.decision.matrix_store import check_items

    items = convert(args.csv, args.legacy)
    warnings = check_items(items)
    args.output.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Предупреждений валидации: {len(warnings)}")
    print(
        f"Сохранено триггеров: {len(items)}; "
        f"врачебных: {sum(t['provenance'] == 'clinician' for t in items)}"
    )


if __name__ == "__main__":
    main()
