from __future__ import annotations

import argparse
import json
from pathlib import Path

from doc_processing.parser_humanized import parse_one as parse_humanized_one

from .pipeline import extract_findings_for_document


def _read_document(path: Path) -> dict:
    if path.suffix.lower() == ".docx":
        return parse_humanized_one(path)
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Извлечь клинические находки через локальную модель Ollama."
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Файл JSON или DOCX со структурированным протоколом.",
    )
    parser.add_argument("--output", type=Path, help="Выходной файл JSON с находками.")
    parser.add_argument("--model", default="medgemma:4b", help="Модель Ollama для извлечения.")
    parser.add_argument(
        "--base-url", default="http://localhost:11434", help="Адрес сервера Ollama."
    )
    args = parser.parse_args()

    document = _read_document(args.input)
    result = extract_findings_for_document(document, base_url=args.base_url, model=args.model)

    output_path = args.output or args.input.with_suffix(".findings.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"findings": len(result.get("findings", []))}, ensure_ascii=False))


if __name__ == "__main__":
    main()
