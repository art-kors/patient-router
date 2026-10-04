"""Проверки импорта врачебных данных и актуальной матрицы."""

import csv
import json
from dataclasses import asdict

from app.services.decision.matrix import _build, load_triggers, validate
from app.services.decision.matrix_store import check_items
from scripts.import_clinical_matrix import (
    CSV_PATH,
    LEGACY_PATH,
    MERGES,
    OUTPUT_PATH,
    convert,
    formal_thresholds,
    identifier,
    split_list,
    study_name,
)


def test_импорт_воспроизводим_и_совпадает_с_конфигом():
    first = convert()
    assert first == convert()
    assert first == json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    assert len(first) == 43
    assert len({t["trigger_id"] for t in first}) == 43
    assert sum(t["provenance"] == "clinician" for t in first) == 36
    assert check_items(first) == validate(load_triggers(OUTPUT_PATH))


def test_отрицания_условия_и_цитаты_не_теряются():
    with CSV_PATH.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    for row, trigger in zip(rows, convert()[:36], strict=True):
        assert trigger["clinical_source"] == row
        assert trigger["trigger_id"] == identifier(row["display_name (Находка)"])
        assert set(split_list(row["negative_contexts (Отрицания/Норма)"])) <= set(
            trigger["negative_contexts"]
        )
        assert trigger["threshold_text"] == row["thresholds (Пороги)"]
        assert trigger["evidence_phrases"] == split_list(row["Фраза-доказательство"], ";")
        restored = asdict(_build(trigger))
        assert restored["clinical_source"] == row
        assert restored["threshold_text"] == trigger["threshold_text"]


def test_объединены_только_явные_пересечения():
    triggers = convert()
    ids = {t["trigger_id"] for t in triggers}
    legacy = json.loads(LEGACY_PATH.read_text(encoding="utf-8"))
    for old in legacy:
        if old["trigger_id"] in MERGES.values():
            matches = [t for t in triggers if old["trigger_id"] in t.get("legacy_trigger_ids", [])]
            assert len(matches) == 1
            assert old["trigger_id"] not in ids
            assert set(old["negative_contexts"]) <= set(matches[0]["negative_contexts"])
        else:
            assert old["trigger_id"] in ids


def test_нормализация_и_внутренние_списки():
    assert study_name("УЗИ ОРГАНОВ МАЛОГО ТАЗА (матки и придатков)") == "УЗИ органов малого таза"
    assert (
        study_name("Дуплексное исследование артерий нижних конечностей")
        == "УЗДГ артерий нижних конечностей"
    )
    assert split_list('норма, "просвет анэхогенный, однородный"') == [
        "норма",
        "просвет анэхогенный, однородный",
    ]
    assert split_list("первая цитата;вторая цитата", ";") == ["первая цитата", "вторая цитата"]


def test_не_выдумываем_порог_из_альтернативы():
    assert formal_thresholds("диаметр БПВ > 5 мм / наличие варикозной трансформации") == {}
    assert formal_thresholds("диаметр ≥ 5 мм") == {"min_size_mm": 5}
    assert formal_thresholds("стеноз ≥ 70%") == {"min_stenosis_percent": 70}
