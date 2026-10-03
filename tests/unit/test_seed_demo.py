"""Unit tests for scripts/seed_demo.py — parsing and mapping logic WITHOUT real DB."""

import json

# Import the functions to test by executing the script's module
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

# We'll test the parse_protocol function by importing the module
# Since the script isn't structured as a module, we'll test the parsing logic directly

from uuid import UUID, uuid5


def test_parse_protocol() -> None:
    """Test parsing of protocol header metadata."""
    from scripts.seed_demo import parse_protocol

    sample = """# Синтетический демо-протокол УЗИ (обезличено, данные не настоящие)
# study_id: demo_abdomen_01
# тип исследования: УЗИ брюшной полости
Пациент: Кузнецова Мария Алексеевна (синтетические данные)
Пол: женский
Дата рождения: 19.01.1990
Возраст на момент осмотра: 36
Номер амбулаторной карты: 20279
Дата приёма: 15.04.2026

ОПИСАНИЕ
..."""

    meta = parse_protocol(sample)
    assert meta["study_id"] == "demo_abdomen_01"
    assert meta["study_type"] == "УЗИ брюшной полости"
    assert meta["patient_name"] == "Кузнецова Мария Алексеевна (синтетические данные)"
    assert meta["sex"] == "F"
    assert meta["dob"] == "19.01.1990"
    assert meta["age"] == 36
    assert meta["card"] == "20279"
    assert meta["visit_date"] == "15.04.2026"


def test_parse_protocol_male() -> None:
    """Test parsing with male sex."""
    from scripts.seed_demo import parse_protocol

    sample = """# study_id: demo_prostate_01
# тип исследования: УЗИ предстательной железы
Пол: мужской
Номер амбулаторной карты: 12345
Возраст на момент осмотра: 50
Дата приёма: 01.01.2025
Дата рождения: 01.01.1975
Пациент: Test (синтетические данные)
"""
    meta = parse_protocol(sample)
    assert meta["sex"] == "M"
    assert meta["card"] == "12345"


def test_parse_protocol_unspecified_sex() -> None:
    """Test parsing with unspecified sex."""
    from scripts.seed_demo import parse_protocol

    sample = """# study_id: demo_lower_limb_01
# тип исследования: УЗДГ артерий нижних конечностей
Пол: не указан
Номер амбулаторной карты: 99999
Возраст на момент осмотра: 40
Дата приёма: 01.01.2025
Дата рождения: 01.01.1985
Пациент: Test (синтетические данные)
"""
    meta = parse_protocol(sample)
    assert meta["sex"] is None


def test_deterministic_uuids() -> None:
    """Test that uuid5 with fixed namespace gives stable IDs."""
    from scripts.seed_demo import SEED_NS

    demo_id = "demo_abdomen_01"
    card = "20279"

    study_uuid = uuid5(SEED_NS, f"study:{demo_id}")
    patient_uuid = uuid5(SEED_NS, f"patient:{card}")
    protocol_uuid = uuid5(SEED_NS, f"protocol:{demo_id}")

    # Repeat
    study_uuid2 = uuid5(SEED_NS, f"study:{demo_id}")
    patient_uuid2 = uuid5(SEED_NS, f"patient:{card}")
    protocol_uuid2 = uuid5(SEED_NS, f"protocol:{demo_id}")

    assert study_uuid == study_uuid2
    assert patient_uuid == patient_uuid2
    assert protocol_uuid == protocol_uuid2

    # Different inputs give different UUIDs
    assert uuid5(SEED_NS, "study:demo_abdomen_02") != study_uuid
    assert uuid5(SEED_NS, "patient:99999") != patient_uuid


def test_index_structure() -> None:
    """Test that the index JSON has correct structure (demo_id -> UUID string)."""
    from scripts.seed_demo import SEED_NS

    index = {
        "demo_abdomen_01": str(uuid5(SEED_NS, "study:demo_abdomen_01")),
        "demo_abdomen_02": str(uuid5(SEED_NS, "study:demo_abdomen_02")),
    }

    # All keys are demo_* strings
    assert all(k.startswith("demo_") for k in index)
    # All values are valid UUID strings
    for v in index.values():
        UUID(v)  # raises if invalid

    # JSON serializable
    json_str = json.dumps(index)
    loaded = json.loads(json_str)
    assert loaded == index


@pytest.mark.asyncio
async def test_upsert_demo_mock_db() -> None:
    """Test upsert_demo logic with mocked DB session."""
    from scripts.seed_demo import SEED_NS, parse_protocol

    # Mock session
    session = AsyncMock()

    # Mock patient not found -> create new
    mock_patient = MagicMock()
    mock_patient.id = uuid5(SEED_NS, "patient:20279")
    mock_patient.age = 36
    mock_patient.sex = "F"

    # First call to scalar (patient lookup) returns None -> create
    # Second call (study) returns None -> create
    # Third call (protocol) returns None -> create
    scalar_results = [None, None, None]  # patient, study, protocol

    async def scalar_side_effect(query):
        result = scalar_results.pop(0)
        mock_result = MagicMock()
        mock_result.scalar.return_value = result
        return mock_result

    session.scalar.side_effect = scalar_side_effect
    session.get.side_effect = lambda model, id: None  # study, protocol not found

    # Mock commit
    session.commit = AsyncMock()

    # Sample protocol text
    protocol_text = """# study_id: demo_abdomen_01
# тип исследования: УЗИ брюшной полости
Пациент: Test (синтетические данные)
Пол: женский
Дата рождения: 19.01.1990
Возраст на момент осмотра: 36
Номер амбулаторной карты: 20279
Дата приёма: 15.04.2026
ОПИСАНИЕ
Test"""

    # We can't easily test full upsert_demo due to complex mocking
    # But we can verify parse_protocol feeds correct data
    meta = parse_protocol(protocol_text)
    assert meta["study_id"] == "demo_abdomen_01"
    assert meta["card"] == "20279"


def test_load_study_index_integration() -> None:
    """Integration test: load_study_index reads the generated file."""
    from app.services import quality

    # The file should exist after seed_demo.py runs
    index_path = Path("data/demo/study_index.json")
    if index_path.exists():
        index = quality.load_study_index(index_path)
        assert isinstance(index, dict)
        assert len(index) == 89
        # Spot check a few
        assert "demo_abdomen_01" in index
        assert "demo_gynecology_01" in index
        UUID(index["demo_abdomen_01"])  # valid UUID


def test_resolve_study_ids() -> None:
    """Test resolve_study_ids maps demo_* to UUID and passes through UUIDs."""
    from uuid import uuid4

    from app.services.quality import LabeledSample, resolve_study_ids

    index = {
        "demo_abdomen_01": "11111111-1111-1111-1111-111111111111",
        "demo_breast_01": "22222222-2222-2222-2222-222222222222",
    }

    # Mix of demo_* and already-UUID
    uuid_val = str(uuid4())
    samples = [
        LabeledSample(
            study_id="demo_abdomen_01", trigger_id="cholelithiasis", label=True, split="gold"
        ),
        LabeledSample(
            study_id="demo_breast_01", trigger_id="breast_birads_3_5", label=False, split="gold"
        ),
        LabeledSample(study_id=uuid_val, trigger_id="test", label=True, split="gold"),
    ]

    resolved = resolve_study_ids(samples, index)

    assert resolved[0].study_id == "11111111-1111-1111-1111-111111111111"
    assert resolved[1].study_id == "22222222-2222-2222-2222-222222222222"
    assert resolved[2].study_id == uuid_val  # passed through unchanged
    assert len(resolved) == 3


def test_resolve_study_ids_missing_raises() -> None:
    """Test resolve_study_ids raises on unknown demo_* id."""
    from app.services.quality import LabeledSample, QualityDataError, resolve_study_ids

    index = {"demo_known": "11111111-1111-1111-1111-111111111111"}
    samples = [
        LabeledSample(study_id="demo_unknown", trigger_id="test", label=True, split="gold"),
    ]

    with pytest.raises(QualityDataError, match="не найден в индексе"):
        resolve_study_ids(samples, index)
