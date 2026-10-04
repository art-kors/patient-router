from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

try:
    from doc_processing.parser_humanized import parse_one as parse_humanized_one
except ModuleNotFoundError:
    try:
        from parser_humanized import parse_one as parse_humanized_one
    except ModuleNotFoundError:
        ready_to_run_root = Path(__file__).resolve().parents[1]
        if str(ready_to_run_root) not in sys.path:
            sys.path.insert(0, str(ready_to_run_root))
        from parser_humanized import parse_one as parse_humanized_one

from .client import OllamaClient
from .extractor import prepare_document_for_llm


def extract_findings_for_document(
    document: dict[str, Any],
    *,
    base_url: str = "http://localhost:11434",
    model: str = "medgemma:4b",
    timeout: int = 120,
) -> dict[str, Any]:
    client = OllamaClient(base_url=base_url, model=model, timeout=timeout)
    document_for_llm = prepare_document_for_llm(document)
    return client.extract_findings(document_for_llm)


def run_llm_pipeline(
    input_path: str | Path,
    *,
    output_path: str | Path | None = None,
    base_url: str = "http://localhost:11434",
    model: str = "medgemma:4b",
    timeout: int = 120,
) -> dict[str, Any]:
    source = Path(input_path)
    if not source.exists():
        raise FileNotFoundError(f"Input path does not exist: {source}")

    if source.suffix.lower() == ".docx":
        payload = parse_humanized_one(source)
    else:
        payload = json.loads(source.read_text(encoding="utf-8"))

    result = extract_findings_for_document(
        payload,
        base_url=base_url,
        model=model,
        timeout=timeout,
    )

    destination = Path(output_path) if output_path is not None else source.with_suffix(".findings.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
