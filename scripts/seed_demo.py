#!/usr/bin/env python3
"""Сид для демо-стенда: создаёт Patient/Study/Protocol в БД из data/demo/protocols/*.txt
и пишет data/demo/study_index.json — маппинг demo_* -> UUID.

Идемпотентно: использует детерминированные UUID (uuid5 от фиксированного namespace).
Повторный запуск не дублирует записи.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionFactory
from app.models import Patient, Protocol, Study, StudyStatus

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_DIR = REPO_ROOT / "data" / "demo"
PROTOCOLS_DIR = DEMO_DIR / "protocols"
INDEX_PATH = DEMO_DIR / "study_index.json"

# Фиксированный namespace для детерминированных UUID
SEED_NS = UUID("6e8b7c12-4d3a-5f9e-8b2c-1a7f4e9d3c6b")

# Парсеры заголовков
RE_STUDY_ID = re.compile(r"^# study_id: (\S+)$", re.M)
RE_STUDY_TYPE = re.compile(r"^# тип исследования: (.+)$", re.M)
RE_PATIENT = re.compile(r"^Пациент: (.+)$", re.M)
RE_SEX = re.compile(r"^Пол: (.+)$", re.M)
RE_DOB = re.compile(r"^Дата рождения: (.+)$", re.M)
RE_AGE = re.compile(r"^Возраст на момент осмотра: (\d+)$", re.M)
RE_CARD = re.compile(r"^Номер амбулаторной карты: (\S+)$", re.M)
RE_VISIT_DATE = re.compile(r"^Дата приёма: (.+)$", re.M)

SEX_MAP = {"женский": "F", "мужской": "M", "не указан": None}


def parse_protocol(text: str) -> dict:
    """Извлечь метаданные из текста протокола."""
    return {
        "study_id": RE_STUDY_ID.search(text).group(1).strip(),
        "study_type": RE_STUDY_TYPE.search(text).group(1).strip(),
        "patient_name": RE_PATIENT.search(text).group(1).strip(),
        "sex": SEX_MAP.get(RE_SEX.search(text).group(1).strip()),
        "dob": RE_DOB.search(text).group(1).strip(),
        "age": int(RE_AGE.search(text).group(1)),
        "card": RE_CARD.search(text).group(1).strip(),
        "visit_date": RE_VISIT_DATE.search(text).group(1).strip(),
    }


async def upsert_demo(session: AsyncSession) -> dict[str, str]:
    """Создать/обновить записи для всех 89 протоколов. Вернуть маппинг demo_id -> UUID."""
    index: dict[str, str] = {}
    created = updated = 0

    for path in sorted(PROTOCOLS_DIR.glob("*.txt")):
        text = path.read_text(encoding="utf-8")
        meta = parse_protocol(text)
        demo_id = meta["study_id"]

        # Детерминированные UUID
        study_uuid = uuid5(SEED_NS, f"study:{demo_id}")
        patient_uuid = uuid5(SEED_NS, f"patient:{meta['card']}")
        protocol_uuid = uuid5(SEED_NS, f"protocol:{demo_id}")

        # Patient: upsert by external_id (card)
        card = meta["card"]
        patient = await session.scalar(select(Patient).where(Patient.external_id == card))
        if patient is None:
            patient = Patient(
                id=patient_uuid,
                external_id=card,
                anonymized_hash=card,  # для демо хватает
                age=meta["age"],
                sex=meta["sex"],
            )
            session.add(patient)
            created += 1
        else:
            # обновляем age/sex если изменились
            if patient.age != meta["age"] or patient.sex != meta["sex"]:
                patient.age = meta["age"]
                patient.sex = meta["sex"]
                updated += 1

        # Study: upsert by id
        study = await session.get(Study, study_uuid)
        if study is None:
            # парсим дату приёма как study_date
            visit = datetime.strptime(meta["visit_date"], "%d.%m.%Y").date()
            study = Study(
                id=study_uuid,
                patient_id=patient.id,
                study_type=meta["study_type"],
                study_date=visit,
                document_ref=demo_id,
                raw_text=text,
                status=StudyStatus.RECEIVED.value,
            )
            session.add(study)
            created += 1
        else:
            # raw_text может обновиться при регенерации протоколов
            if study.raw_text != text or study.study_type != meta["study_type"]:
                study.raw_text = text
                study.study_type = meta["study_type"]
                updated += 1

        # Protocol: upsert by id
        protocol = await session.get(Protocol, protocol_uuid)
        if protocol is None:
            visit_dt = datetime.strptime(meta["visit_date"], "%d.%m.%Y")
            protocol = Protocol(
                id=protocol_uuid,
                study_id=study.id,
                signed_at=visit_dt,
                facts={},
            )
            session.add(protocol)
            created += 1
        else:
            updated += 1

        index[demo_id] = str(study_uuid)

    await session.commit()
    print(f"Created: {created}, Updated: {updated}, Total protocols: {len(index)}")
    return index


async def main() -> int:
    async with SessionFactory() as session:
        index = await upsert_demo(session)

    INDEX_PATH.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Index written to {INDEX_PATH} ({len(index)} entries)")
    return 0


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(main()))
