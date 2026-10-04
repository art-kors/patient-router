"""Журнал без персональных данных и оценка по независимой врачебной разметке."""

import json
from collections import defaultdict
from dataclasses import asdict
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.clock import get_clock
from app.models import AnalysisRun
from app.services.decision.engine import DecisionEngine
from app.services.extraction.base import Finding


def digest(value):
    """Хэш для сопоставления протоколов без хранения исходного текста."""
    return sha256(value.encode()).hexdigest()


def safe_findings(response):
    """Белый список признаков: произвольные строки декодера не сохраняются."""
    triggers = response._triggers
    result = []
    for f in response.findings:
        ids = [t.trigger_id for t in triggers if t.matches(f.finding)]
        result.append(
            {
                "trigger_ids": ids,
                "finding_hash": digest(f.finding),
                "size_mm": f.size_mm,
                "in_negative_context": f.in_negative_context,
                "confidence": f.confidence,
            }
        )
    return result


def rule_signature(trigger):
    """Отпечаток условий, которые нельзя восстановить по одному размеру."""
    values = asdict(trigger)
    values.pop("thresholds")
    values.pop("version")
    return digest(json.dumps(values, sort_keys=True, ensure_ascii=False))


def safe_matches(response):
    """Решения и версии без цитат; отпечаток защищает честность переоценки."""
    fields = ("trigger_id", "fired", "suppressed", "suppression_reason", "version")
    triggers = {t.trigger_id: t for t in response._triggers}
    result = []
    for match in response.matches:
        values = {k: getattr(match, k) for k in fields}
        trigger = triggers[match.trigger_id]
        values["rule_signature"] = rule_signature(trigger)
        values["thresholds"] = trigger.thresholds
        values["display_name"] = trigger.display_name
        values["winning"] = match.trigger_id == response._winner_trigger_id
        values["emergency"] = trigger.emergency_flag
        result.append(values)
    return result


async def record(session, response, text, started, request_key=None):
    """Записать разбор атомарно; повтор ключа возвращает исходный разбор."""
    now = get_clock().now()
    values = dict(
        id=uuid4(),
        request_key=digest(request_key) if request_key else None,
        text_hash=digest(text),
        text_length=len(text),
        study_type=response.study_type
        if response.study_type in {t.source_study for t in response._triggers}
        else None,
        created_at=now,
        duration_ms=max(0, (get_clock().monotonic() - started) * 1000),
        decoder_used=response.decoder_used,
        fallback=response.llm_error is not None,
        findings=safe_findings(response),
        matches=safe_matches(response),
    )
    stmt = (
        insert(AnalysisRun)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[AnalysisRun.request_key])
        .returning(AnalysisRun.id)
    )
    identifier = (await session.execute(stmt)).scalar_one_or_none()
    if identifier is None:
        old = await session.scalar(
            select(AnalysisRun).where(AnalysisRun.request_key == values["request_key"])
        )
        if old.text_hash != values["text_hash"] or old.study_type != values["study_type"]:
            raise ValueError("Ключ повторного запроса уже использован для другого протокола")
        if (
            old.matches != values["matches"]
            or old.findings != values["findings"]
            or old.decoder_used != values["decoder_used"]
        ):
            raise ValueError("Результат разбора изменился; повторите запрос с новым ключом")
        identifier = old.id
    await session.commit()
    return identifier


def replay(run, engine):
    """Переоценить только восстановимые изменения порога размера.

    Неизменённое правило сохраняет исходное решение: потеря текстовых
    признаков не должна изображать ухудшение или улучшение. Остальные
    изменения помечаются как требующие нового разбора исходного текста.
    """
    original = {m["trigger_id"]: m for m in run.matches}
    result = []
    for trigger in engine.triggers:
        old = original.get(trigger.trigger_id)
        if old and old.get("rule_signature") == rule_signature(trigger):
            if old.get("thresholds") == trigger.thresholds:
                result.append(dict(old, reanalysis_required=False))
                continue
            keys = set(trigger.thresholds) | set(old.get("thresholds", {}))
            if keys <= {"min_size_mm", "max_size_mm"}:
                findings = [
                    Finding(
                        finding=trigger.synonyms[0],
                        quote="Обезличенный признак",
                        size_mm=f["size_mm"],
                        confidence=f["confidence"],
                        in_negative_context=f["in_negative_context"]
                        or old.get("suppression_reason") == "negative_context",
                    )
                    for f in run.findings
                    if trigger.trigger_id in f["trigger_ids"]
                ]
                decision = DecisionEngine([trigger]).decide(findings, study_type=run.study_type)
                result.append(
                    {
                        "trigger_id": trigger.trigger_id,
                        "fired": decision.matches[0].fired,
                        "reanalysis_required": False,
                    }
                )
                continue
        result.append(
            {
                "trigger_id": trigger.trigger_id,
                "fired": old["fired"] if old else False,
                "reanalysis_required": True,
            }
        )
    return result


def metrics(counts):
    """Пустой знаменатель означает недостаточность данных, а не плохое качество."""
    tp, fp, fn, tn = (counts[k] for k in ("tp", "fp", "fn", "tn"))
    return dict(
        counts,
        recall=tp / (tp + fn) if tp + fn else None,
        precision=tp / (tp + fp) if tp + fp else None,
        error_rate=(fp + fn) / (tp + fp + fn + tn) if tp + fp + fn + tn else None,
    )


def aggregate(runs, feedback, *, current=False, engine=None, generated=None):
    """Врач заменяет сгенерированную метку той же пары; непроверенные не считаются TP."""
    labels = dict(generated or {})
    doctors = {(str(f.analysis_id), f.trigger_id): f.label for f in feedback}
    labels.update({key: value != "false_positive" for key, value in doctors.items()})
    rows = {}
    profiles = {}
    timeline = {}
    needs_reanalysis = 0
    total = dict.fromkeys(("tp", "fp", "fn", "tn"), 0)
    for run in runs:
        profiles.setdefault(run.study_type or "Не указан", defaultdict(int))
        matches = replay(run, engine or DecisionEngine()) if current else run.matches
        needs_reanalysis += sum(m.get("reanalysis_required", False) for m in matches)
        predicted = {m["trigger_id"]: m["fired"] for m in matches}
        ids = set(predicted) | {t for r, t in labels if r == str(run.id)}
        for tid in ids:
            row = rows.setdefault(
                tid,
                dict(
                    trigger_id=tid,
                    occurrences=0,
                    fired=0,
                    confirmed=0,
                    rejected=0,
                    missed=0,
                    reviewed=0,
                    **dict.fromkeys(("tp", "fp", "fn", "tn"), 0),
                ),
            )
            fired = predicted.get(tid, False)
            label = labels.get((str(run.id), tid))
            present = next((m for m in run.matches if m["trigger_id"] == tid), {})
            row["display_name"] = present.get("display_name", tid)
            row["occurrences"] += int(
                fired
                or label is True
                or present.get("suppression_reason") in ("negative_context", "threshold_not_met")
            )
            row["fired"] += int(fired)
            doctor = doctors.get((str(run.id), tid))
            row["confirmed"] += int(doctor == "confirmed")
            row["rejected"] += int(doctor == "false_positive")
            row["missed"] += int(doctor == "missed")
            row["reviewed"] += int(doctor is not None)
            if label is None:
                continue
            key = ("tp" if label else "fp") if fired else ("fn" if label else "tn")
            row[key] += 1
            total[key] += 1
            profile = profiles.setdefault(run.study_type or "Не указан", defaultdict(int))
            profile[key] += 1
            if getattr(run, "stored", True):
                day = run.created_at.date().isoformat()
                bucket = timeline.setdefault(day, defaultdict(int))
                bucket[key] += 1
    if engine is not None:
        for trigger in engine.triggers:
            rows.setdefault(
                trigger.trigger_id,
                dict(
                    trigger_id=trigger.trigger_id,
                    occurrences=0,
                    fired=0,
                    confirmed=0,
                    rejected=0,
                    missed=0,
                    reviewed=0,
                    **dict.fromkeys(("tp", "fp", "fn", "tn"), 0),
                ),
            )
        for trigger in engine.triggers:
            rows[trigger.trigger_id]["display_name"] = trigger.display_name
    reviewed = len(doctors)
    rejected = sum(v == "false_positive" for v in doctors.values())
    checked_findings = sum(v != "missed" for v in doctors.values())
    return {
        "analyses": len(runs),
        "reanalysis_required_pairs": needs_reanalysis,
        "metrics": metrics(total),
        "reviewed": reviewed,
        "rejected_fraction": rejected / checked_findings if checked_findings else None,
        "feedback_rejected_fraction": rejected / reviewed if reviewed else None,
        "by_trigger": sorted(
            (
                dict(
                    row,
                    **{
                        k: v
                        for k, v in metrics({k: row[k] for k in total}).items()
                        if k not in total
                    },
                )
                for row in rows.values()
            ),
            key=lambda row: (row["error_rate"] is None, -(row["error_rate"] or 0)),
        ),
        "by_study": [
            {"study_type": name, **metrics({k: c[k] for k in total})}
            for name, c in profiles.items()
        ],
        "timeline": [
            {"date": day, **metrics({k: c[k] for k in total})}
            for day, c in sorted(timeline.items())
        ],
        "decoders": {
            name: sum(r.decoder_used == name for r in runs)
            for name in {r.decoder_used for r in runs}
        },
    }
