"""Проверки внешней МИС без настоящей БД и клинических подсказок."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4, uuid5

import pytest

from app.api.mock_mis import EmitIn
from app.services.mis import EVENT_TYPES
from app.services.mock_mis import SEED_NS, MockMisService


@pytest.fixture
def service(tmp_path):
    """Создать два документа одного синтетического пациента."""
    for index, conclusion in enumerate(("Патологии не выявлено", "Полип эндометрия"), 1):
        (tmp_path / f"demo_{index}.txt").write_text(
            f"# study_id: demo_{index}\n# тип исследования: УЗИ органов малого таза\n"
            "Пациент: Тестовая Анна (синтетические данные)\nПол: женский\n"
            "Возраст на момент осмотра: 40\nНомер амбулаторной карты: 123\n"
            f"Дата приёма: 01.09.2026\n\nЗАКЛЮЧЕНИЕ\n{conclusion}\n",
            encoding="utf-8",
        )
    return MockMisService(tmp_path)


@pytest.fixture
def session(service):
    """Сессия, возвращающая пациента штатного демо-сида."""
    value = Mock()
    value.scalar = AsyncMock(return_value=SimpleNamespace(id=service.catalog()[0]["patient_id"]))
    value.flush = AsyncMock()
    value.commit = AsyncMock()
    value.rollback = AsyncMock()
    return value


def test_catalog_groups_patient_and_preserves_norm(service):
    patients = service.catalog()
    assert len(patients) == 1
    assert len(patients[0]["studies"]) == 2
    assert patients[0]["patient_id"] == uuid5(SEED_NS, "patient:123")
    assert patients[0]["studies"][0]["id"] == uuid5(SEED_NS, "study:demo_1")
    assert "Патологии не выявлено" in patients[0]["studies"][0]["text"]
    assert "trigger" not in str(patients)


def test_missing_catalog_is_explicit(tmp_path):
    with pytest.raises(FileNotFoundError):
        MockMisService(tmp_path).catalog()


async def test_norm_delivered_unchanged_and_no_invented_route(service, session, monkeypatch):
    handler = AsyncMock(return_value={"actions": [], "route_id": None, "status": "processed"})
    monkeypatch.setattr("app.services.mock_mis.MisEventHandler.handle", handler)
    result = await service.emit(session, "StudyProtocolSigned", EmitIn(study_id="demo_1"))
    event = handler.call_args.args[1]
    assert event["payload"]["text"] == service.catalog()[0]["studies"][0]["text"]
    assert event["payload"]["study_date"] == "2026-09-01"
    assert result["route_created"] is False
    session.commit.assert_awaited_once()


@pytest.mark.parametrize("event_type", list(EVENT_TYPES))
async def test_all_event_types_use_real_handler(service, session, monkeypatch, event_type):
    route_id = uuid4()
    patient_id = service.catalog()[0]["patient_id"]
    session.scalar.side_effect = (
        [SimpleNamespace(id=patient_id)]
        if event_type.startswith("StudyProtocol")
        else [SimpleNamespace(id=route_id), SimpleNamespace(id=patient_id)]
    )
    handler = AsyncMock(return_value={"actions": ["route_created: пример"]})
    monkeypatch.setattr("app.services.mock_mis.MisEventHandler.handle", handler)
    result = await service.emit(
        session, event_type, EmitIn(study_id="demo_2", payload={"tactics": "observation"})
    )
    event = handler.call_args.args[1]
    assert event["event_type"] == event_type
    if not event_type.startswith("StudyProtocol"):
        assert event["subject"]["route_id"] == str(route_id)
        assert "route.created_at DESC" in str(session.scalar.call_args_list[0].args[0])
    assert result["route_created"] is True


@pytest.mark.parametrize(
    "body,event_type,error",
    [
        (EmitIn(study_id="unknown"), "StudyProtocolSigned", LookupError),
        (EmitIn(study_id="demo_1", patient_id=uuid4()), "StudyProtocolSigned", ValueError),
        (EmitIn(study_id="demo_1"), "Unknown", ValueError),
        (EmitIn(study_id="demo_1"), "TacticsChosen", ValueError),
    ],
)
async def test_invalid_selection_never_delivered(service, session, body, event_type, error):
    with pytest.raises(error):
        await service.emit(session, event_type, body)
    session.commit.assert_not_awaited()


async def test_queue_distinguishes_ready_processed_and_findings(service, session):
    study = service.catalog()[0]["studies"][0]
    event = SimpleNamespace(
        event_id="mock-mis:1",
        event_type="StudyProtocolSigned",
        occurred_at=None,
        processed_at=None,
        patient_id=None,
        study_id=study["id"],
        payload={},
    )
    finding = SimpleNamespace(
        id=uuid4(), study_id=study["id"], finding="Полип", quote="Полип эндометрия", created_at=None
    )
    session.scalars = AsyncMock(
        side_effect=[
            Mock(all=lambda: [study["id"]]),
            Mock(all=lambda: [event]),
            Mock(all=lambda: [event]),
            Mock(all=lambda: [finding]),
        ]
    )
    result = await service.queue(session, 100)
    assert result["ready_count"] == 1
    assert result["ready_studies"][0]["study_id"] == "demo_2"
    assert len(result["unprocessed_events"]) == 1
    assert result["fresh_findings"][0]["quote"] == "Полип эндометрия"
    assert result["findings_source"] == "patient-router"


async def test_new_patient_created_before_delivery(service, session, monkeypatch):
    session.scalar.return_value = None
    handler = AsyncMock(return_value={"actions": []})
    monkeypatch.setattr("app.services.mock_mis.MisEventHandler.handle", handler)
    await service.emit(session, "StudyProtocolSigned", EmitIn(study_id="demo_1"))
    patient = session.add.call_args.args[0]
    assert patient.id == service.catalog()[0]["patient_id"]
    session.flush.assert_awaited_once()


async def test_missing_route_does_not_create_fake_route(service, session):
    session.scalar.return_value = None
    with pytest.raises(LookupError, match="Маршрут"):
        await service.emit(session, "AppointmentBooked", EmitIn(study_id="demo_1"))
    session.add.assert_not_called()
    session.commit.assert_not_awaited()


async def test_corrected_text_is_not_replaced(service, session, monkeypatch):
    handler = AsyncMock(return_value={"actions": []})
    monkeypatch.setattr("app.services.mock_mis.MisEventHandler.handle", handler)
    await service.emit(
        session,
        "StudyProtocolCorrected",
        EmitIn(study_id="demo_1", payload={"raw_text": "Исправленный протокол"}),
    )
    assert handler.call_args.args[1]["payload"]["text"] == "Исправленный протокол"
