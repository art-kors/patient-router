from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from doc_processing.llm.pipeline import extract_findings_for_document
from doc_processing.parsing.pipeline import process_one_document


def _ollama_ready(host: str) -> bool:
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=2) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def ensure_ollama(model_name: str = "medgemma:4b", host: str = "http://127.0.0.1:11434") -> bool:
    if _ollama_ready(host):
        return True

    subprocess.run(
        ["bash", "-lc", "apt-get update -qq && apt-get install -y -qq zstd curl"],
        check=True,
    )
    subprocess.run(
        ["bash", "-lc", "curl -fsSL https://ollama.com/install.sh | sh"],
        check=True,
    )

    process = subprocess.Popen(
        ["ollama", "serve"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )

    for _ in range(60):
        if _ollama_ready(host):
            break
        time.sleep(2)
    else:
        process.terminate()
        raise RuntimeError("Ollama не запустился. Проверьте доступность порта 11434.")

    subprocess.run(["ollama", "pull", model_name], check=True)
    return True


def process_report(
    file_path: str | Path,
    *,
    mode: str = "full",
    with_llm: bool = False,
    model_name: str = "medgemma:4b",
    host: str = "http://127.0.0.1:11434",
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Обработать один файл протокола.

    Режим full или compact определяет структуру результата парсера.
    Параметр with_llm добавляет находки Ollama для выбранного файла.
    """
    source = Path(file_path)
    if not source.exists():
        raise FileNotFoundError(f"Входной файл не существует: {source}")

    payload = process_one_document(source, output_mode=mode)

    result: dict[str, Any] = {
        "document": str(source),
        "mode": mode,
        "data": payload,
    }

    if with_llm:
        ensure_ollama(model_name=model_name, host=host)
        findings = extract_findings_for_document(
            payload,
            base_url=host,
            model=model_name,
        )
        result["findings"] = findings.get("findings", [])

    if output_path is not None:
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    return result
