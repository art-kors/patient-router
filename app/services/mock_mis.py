"""Внешняя демо-МИС: каталог протоколов и доставка фактов без знания триггеров."""

from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from sqlalchemy import or_, select

from app.clock import get_clock
from app.models import Finding, MisEvent, Patient, Route
from app.services.mis import EVENT_TYPES, MisEventHandler

PROTOCOLS_DIR = Path(__file__).resolve().parents[2] / "data/demo/protocols"
# Совпадает с namespace штатного scripts/seed_demo.py.
SEED_NS = UUID("6e8b7c12-4d3a-5f9e-8b2c-1a7f4e9d3c6b")


class MockMisService:
    """Передавать исходные документы; клинические решения принимает только обработчик."""

    def __init__(self, protocols_dir: Path = PROTOCOLS_DIR):
        self.protocols_dir = protocols_dir

    def catalog(self) -> list[dict]:
        """Прочитать все протоколы без разметки, матрицы и извлечения находок."""
        paths = sorted(self.protocols_dir.glob("*.txt"))
        if not paths:
            raise FileNotFoundError("Демо-протоколы data/demo/protocols/*.txt отсутствуют")
        patients = {}
        seen = set()
        for path in paths:
            raw = path.read_text(encoding="utf-8")
            headers = dict(line.split(": ", 1) for line in raw.splitlines() if ": " in line)
            try:
                demo_id = headers["# study_id"]
                card = headers["Номер амбулаторной карты"]
                patient_id = uuid5(SEED_NS, f"patient:{card}")
                study = {
                    "study_id": demo_id,
                    "id": uuid5(SEED_NS, f"study:{demo_id}"),
                    "study_type": headers["# тип исследования"],
                    "study_date": datetime.strptime(headers["Дата приёма"], "%d.%m.%Y").date(),
                    "text": raw,
                }
                patient = {
                    "patient_id": patient_id,
                    "external_id": card,
                    "name": headers["Пациент"],
                    "age": int(headers["Возраст на момент осмотра"]),
                    "sex": {"женский": "F", "мужской": "M"}.get(headers["Пол"]),
                    "studies": [],
                }
            except (KeyError, ValueError) as exc:
                raise ValueError(f"Некорректный заголовок демо-протокола: {path.name}") from exc
            if demo_id in seen:
                raise ValueError(f"Повторный study_id: {demo_id}")
            seen.add(demo_id)
            patients.setdefault(patient_id, patient)["studies"].append(study)
        return list(patients.values())

    async def emit(self, session, event_type: str, request) -> dict:
        """Доставить событие через штатную транзакционную точку приёма МИС."""
        if event_type not in EVENT_TYPES:
            raise ValueError("Неизвестный тип события МИС")
        catalog = self.catalog()
        patient = study = None
        if request.study_id:
            for candidate in catalog:
                for item in candidate["studies"]:
                    if request.study_id in (item["study_id"], str(item["id"])):
                        patient, study = candidate, item
            if study is None:
                raise LookupError("Исследование отсутствует в демо-каталоге")
        elif request.patient_id:
            patient = next((p for p in catalog if p["patient_id"] == request.patient_id), None)
        if patient is None:
            raise LookupError("Пациент отсутствует в демо-каталоге")
        if request.patient_id and request.patient_id != patient["patient_id"]:
            raise ValueError("Исследование принадлежит другому пациенту")
        if event_type.startswith("StudyProtocol") and study is None:
            raise ValueError("Для события протокола требуется study_id")
        payload = request.payload.copy()
        if event_type in ("StudyProtocolSigned", "StudyProtocolCorrected"):
            payload.setdefault("text", payload.get("raw_text", study["text"]))
            payload.setdefault("study_type", study["study_type"])
            payload.setdefault("study_date", study["study_date"].isoformat())
        if event_type == "TacticsChosen" and "tactics" not in payload:
            raise ValueError("Укажите выбранную врачом тактику в payload.tactics")
        route_id = request.route_id
        if not event_type.startswith("StudyProtocol"):
            query = select(Route).where(Route.patient_id == patient["patient_id"])
            if route_id:
                query = query.where(Route.id == route_id)
            route = await session.scalar(query.order_by(Route.created_at.desc(), Route.id).limit(1))
            if route is None:
                raise LookupError("Маршрут пациента не найден")
            route_id = route.id
        # Пациент должен существовать до вставки исследования и связей события.
        existing = await session.scalar(
            select(Patient).where(Patient.external_id == patient["external_id"])
        )
        if existing is None:
            existing = Patient(
                id=patient["patient_id"],
                external_id=patient["external_id"],
                anonymized_hash=patient["external_id"],
                age=patient["age"],
                sex=patient["sex"],
            )
            session.add(existing)
            await session.flush()
        elif existing.id != patient["patient_id"]:
            raise ValueError("UUID пациента не совпадает с демо-сидом")
        event = {
            "event_id": request.event_id or f"mock-mis:{uuid4()}",
            "event_type": event_type,
            "occurred_at": (request.occurred_at or get_clock().now()).isoformat(),
            "subject": {
                "patient_id": str(patient["patient_id"]),
                "study_id": str(study["id"]) if study else None,
                "route_id": str(route_id) if route_id else None,
            },
            "payload": payload,
        }
        result = await MisEventHandler().handle(session, event)
        await session.commit()
        return {
            **result,
            "route_created": any(
                action.startswith("route_created:") for action in result.get("actions", [])
            ),
        }

    async def queue(self, session, limit: int) -> dict:
        """Показать документы к передаче и обратную связь принимающей системы.

        Находки — результат patient-router, а не предсказание внешней МИС.
        Журнал берётся из БД и сохраняется между перезапусками эмулятора.
        """
        catalog = self.catalog()
        study_ids = [s["id"] for p in catalog for s in p["studies"]]
        demo_event = or_(
            MisEvent.study_id.in_(study_ids),
            MisEvent.patient_id.in_([p["patient_id"] for p in catalog]),
        )
        sent = set(
            (
                await session.scalars(
                    select(MisEvent.study_id).where(
                        MisEvent.study_id.in_(study_ids),
                        MisEvent.event_type == "StudyProtocolSigned",
                    )
                )
            ).all()
        )
        pending = (
            await session.scalars(
                select(MisEvent)
                .where(demo_event, MisEvent.processed_at.is_(None))
                .order_by(MisEvent.occurred_at, MisEvent.id)
                .limit(limit)
            )
        ).all()
        recent = (
            await session.scalars(
                select(MisEvent)
                .where(demo_event)
                .order_by(MisEvent.occurred_at.desc(), MisEvent.id)
                .limit(limit)
            )
        ).all()
        findings = (
            await session.scalars(
                select(Finding)
                .where(Finding.study_id.in_(study_ids))
                .order_by(Finding.created_at.desc(), Finding.id)
                .limit(limit)
            )
        ).all()
        ready = [
            {"patient_id": p["patient_id"], **s}
            for p in catalog
            for s in p["studies"]
            if s["id"] not in sent
        ]

        def event_out(row):
            return {
                key: getattr(row, key)
                for key in (
                    "event_id",
                    "event_type",
                    "occurred_at",
                    "processed_at",
                    "patient_id",
                    "study_id",
                    "payload",
                )
            }

        return {
            "ready_count": len(ready),
            "ready_studies": ready[:limit],
            "unprocessed_events": [event_out(row) for row in pending],
            "recent_events": [event_out(row) for row in recent],
            "fresh_findings": [
                {
                    key: getattr(row, key)
                    for key in ("id", "study_id", "finding", "quote", "created_at")
                }
                for row in findings
            ],
            "findings_source": "patient-router",
        }
