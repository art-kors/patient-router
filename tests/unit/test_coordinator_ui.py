"""Проверка доступности разметки и фактического вывода через Node и минимальный DOM."""

import json
import subprocess
from pathlib import Path

from app.api.analysis import _analyze
from app.services.mock_mis import MockMisService
from tests.integration.test_ui import Elements


def test_actual_script_output(tmp_path):
    catalog = MockMisService().catalog()
    raw = next(s["text"] for p in catalog for s in p["studies"] if "Диагноз" in s["text"])
    text = "Заключение: Полип эндометрия 12 мм."
    analysis = _analyze(text, "УЗИ органов малого таза")
    quote = analysis.findings[0].quote
    protocol = {
        "date": "2026-08-26",
        "study_type": "УЗИ органов малого таза",
        "study_id": "study",
        "findings": [f.model_dump() for f in analysis.findings],
        "rules": [
            {"rule": m.applied_rule, "version": m.version, "fired": m.fired}
            for m in analysis.matches
        ],
        "routes": [],
    }
    data = {
        "trigger": {
            "trigger_id": "test",
            "display_name": "Полип",
            "source_study": "УЗИ",
            "potential_route": "Оперативное лечение показано",
            "synonyms": [],
            "thresholds": {},
            "negative_contexts": [],
            "target_sla_days": 14,
            "priority": 3,
        },
        "raw": raw,
        "catalog": catalog,
        "quote": quote,
        "patients": {
            "items": [
                {
                    "id": "p",
                    "name": "Анна Тестовая",
                    "card": "123",
                    "date": "2026-08-26",
                    "protocol_count": 15,
                }
            ],
            "total": 1,
        },
        "protocols": {"items": [protocol] * 15, "total": 15},
    }
    fixture = tmp_path / "ui.json"
    fixture.write_text(json.dumps(data, default=str), encoding="utf-8")
    result = subprocess.run(
        ["node", "tests/ui/check_scripts.cjs", str(fixture)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_coordinator_keyboard_and_labels():
    parsed = Elements(Path("app/static/coordinator.html").read_text()).elements
    labels = {attrs.get("for") for tag, attrs in parsed if tag == "label"}
    for identifier in ["query", "sort", "file", "study-type"]:
        assert identifier in labels
    assert all(attrs.get("tabindex") != "-1" for _, attrs in parsed)
    assert any(
        tag == "script" and attrs.get("src") == "/static/coordinator.js" and "defer" in attrs
        for tag, attrs in parsed
    )
    assert not any(tag in ["div", "span"] and "onclick" in attrs for tag, attrs in parsed)


def test_empty_auth_users_environment(monkeypatch):
    from app.settings import Settings

    monkeypatch.setenv("AUTH_USERS", "")
    assert Settings(_env_file=None).auth_users == {}
    monkeypatch.setenv("AUTH_USERS", '{"operator":{"role":"coordinator","patient_ids":[]}}')
    assert Settings(_env_file=None).auth_users["operator"]["role"] == "coordinator"
