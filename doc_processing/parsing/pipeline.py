from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from doc_processing.parser_humanized import parse_one as parse_humanized_one

from .extractor import make_report_id, transform_document

JsonDocument = dict[str, Any]
Postprocessor = Callable[[JsonDocument], JsonDocument]
logger = logging.getLogger(__name__)


def _read_json(path: Path) -> JsonDocument:
    if path.suffix.lower() == ".docx":
        payload = parse_humanized_one(path)
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))

    if not isinstance(payload, dict):
        raise ValueError("Верхний уровень JSON должен быть объектом.")
    return payload


def _prepare_result(
    payload: JsonDocument,
    *,
    input_path: Path,
    report_id: str | None,
    output_mode: str,
    postprocess: Postprocessor | None,
) -> JsonDocument:
    if output_mode == "full":
        if not isinstance(payload, dict):
            raise ValueError("Полный режим требует объект документа.")
        if "sections" in payload and "study" in payload:
            result = dict(payload)
            result.setdefault("report_id", report_id or make_report_id(input_path.name))
            if postprocess is not None:
                result = postprocess(result)
                if not isinstance(result, dict):
                    raise TypeError("Обработчик postprocess должен вернуть объект JSON (dict).")
            return result
        raise ValueError("Полный режим ожидает структурированный JSON с study и sections.")

    result = transform_document(
        payload,
        report_id=report_id or make_report_id(input_path.name),
    )
    if postprocess is not None:
        result = postprocess(result)
        if not isinstance(result, dict):
            raise TypeError("Обработчик postprocess должен вернуть объект JSON (dict).")
    return result


def process_one_document(
    input_path: str | Path,
    *,
    report_id: str | None = None,
    postprocess: Postprocessor | None = None,
    output_mode: str = "compact",
) -> JsonDocument:
    """Прочитать и преобразовать один документ.

    Режим compact возвращает клинические блоки для модели.
    Режим full сохраняет секции, находки, отрицания и заключение.
    """
    input_path = Path(input_path)
    payload = _read_json(input_path)
    if input_path.suffix.lower() == ".docx" and output_mode == "full":
        payload = parse_humanized_one(input_path)
    return _prepare_result(
        payload,
        input_path=input_path,
        report_id=report_id,
        output_mode=output_mode,
        postprocess=postprocess,
    )


def _write_json(path: Path, payload: JsonDocument) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def run_pipeline(
    input_dir: str | Path,
    output_dir: str | Path,
    *,
    postprocess: Postprocessor | None = None,
    output_mode: str = "compact",
) -> dict[str, int]:
    input_dir = Path(input_dir).resolve()
    output_dir = Path(output_dir).resolve()

    if not input_dir.exists():
        raise FileNotFoundError(f"Каталог ввода не существует: {input_dir}")
    if not input_dir.is_dir():
        raise NotADirectoryError(f"Путь ввода не является каталогом: {input_dir}")
    if output_dir == input_dir or input_dir in output_dir.parents:
        raise ValueError(
            "Каталог вывода не должен совпадать с каталогом ввода или находиться внутри него."
        )

    input_files = sorted(
        path
        for path in input_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in {".json", ".docx"}
    )
    if not input_files:
        raise ValueError(f"Файлы JSON или DOCX не найдены в {input_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    errors: list[dict[str, str]] = []
    processed = 0

    for input_path in input_files:
        relative_path = input_path.relative_to(input_dir)
        output_path = (output_dir / relative_path).with_suffix(".json")

        try:
            transformed = process_one_document(
                input_path,
                report_id=make_report_id(relative_path),
                postprocess=postprocess,
                output_mode=output_mode,
            )
            _write_json(output_path, transformed)
            processed += 1
            record_key = "blocks" if output_mode == "compact" else "sections"
            record_label = "blocks" if output_mode == "compact" else "sections"
            logger.info(
                f"OK: {relative_path} -> {output_path.relative_to(output_dir)} "
                f"({len(transformed.get(record_key, []))} {record_label})"
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            error = {
                "input": relative_path.as_posix(),
                "error": f"{type(exc).__name__}: {exc}",
            }
            errors.append(error)
            logger.error("Не удалось обработать %s: %s", relative_path, error["error"])

    if errors:
        (output_dir / "_pipeline_errors.json").write_text(
            json.dumps(errors, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    summary = {
        "discovered": len(input_files),
        "processed": processed,
        "errors": len(errors),
    }
    logger.info(
        f"Finished: {summary['processed']}/{summary['discovered']} converted; "
        f"{summary['errors']} errors. Output: {output_dir}"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Подготовить клинические блоки из структурированного JSON протокола."
    )
    parser.add_argument("--input-dir", type=Path, default=Path("output_humanized"))
    parser.add_argument("--output-dir", type=Path, default=Path("model_input"))
    parser.add_argument(
        "--mode",
        choices=("full", "compact"),
        default="full",
        help="full сохраняет секции; compact возвращает блоки для модели.",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s %(name)s: %(message)s",
    )
    run_pipeline(args.input_dir, args.output_dir, output_mode=args.mode)


if __name__ == "__main__":
    main()
