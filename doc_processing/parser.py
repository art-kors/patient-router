from pathlib import Path

from docx import Document
from docx.oxml.ns import qn


def _get_cell_text(tc) -> str:
    """
    Получить текст непосредственно из XML <w:tc>.
    Без дополнительной интерпретации содержимого.
    """
    texts = []

    for paragraph in tc.findall(".//" + qn("w:p")):
        parts = []

        for text_node in paragraph.findall(".//" + qn("w:t")):
            if text_node.text:
                parts.append(text_node.text)

        text = "".join(parts)

        if text:
            texts.append(text)

    return "\n".join(texts).strip()


def _get_grid_span(tc) -> int:
    """
    Количество grid-колонок, занимаемых ячейкой.
    """
    tc_pr = tc.find(qn("w:tcPr"))

    if tc_pr is None:
        return 1

    grid_span = tc_pr.find(qn("w:gridSpan"))

    if grid_span is None:
        return 1

    value = grid_span.get(qn("w:val"), "1")

    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def _get_vmerge(tc):
    """
    Возвращает состояние вертикального объединения:

        None
            vMerge отсутствует

        "restart"
            начало вертикального merge

        "continue"
            продолжение вертикального merge
    """
    tc_pr = tc.find(qn("w:tcPr"))

    if tc_pr is None:
        return None

    v_merge = tc_pr.find(qn("w:vMerge"))

    if v_merge is None:
        return None

    value = v_merge.get(qn("w:val"))

    if value is None:
        return "continue"

    return value


def _get_grid_before(tr) -> int:
    """
    Количество grid-колонок, пропущенных перед первой
    ячейкой строки.
    """
    tr_pr = tr.find(qn("w:trPr"))

    if tr_pr is None:
        return 0

    grid_before = tr_pr.find(qn("w:gridBefore"))

    if grid_before is None:
        return 0

    value = grid_before.get(qn("w:val"), "0")

    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _get_grid_after(tr) -> int:
    """
    Количество grid-колонок, пропущенных после последней
    ячейки строки.
    """
    tr_pr = tr.find(qn("w:trPr"))

    if tr_pr is None:
        return 0

    grid_after = tr_pr.find(qn("w:gridAfter"))

    if grid_after is None:
        return 0

    value = grid_after.get(qn("w:val"), "0")

    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def parse_docx(file_path: str | Path) -> dict:
    """
    Парсит один DOCX.

    Сохраняет:
        document
          └── tables
                └── rows
                      └── cells

    Для каждой физической XML-ячейки сохраняется:

        row
        column
        row_span
        col_span
        text

    Поддерживаются:

        - gridSpan
        - vMerge
        - gridBefore
        - gridAfter

    Важно:
        merged cell не дублируется в JSON.
    """

    file_path = Path(file_path)

    document = Document(file_path)

    result = {
        "file": file_path.name,
        "tables": [],
    }

    for table_index, table in enumerate(document.tables):
        tr_elements = table._tbl.findall(qn("w:tr"))
        rows = []
        active_vertical_merges = {}

        for row_index, tr in enumerate(tr_elements):
            cells = []
            column_index = _get_grid_before(tr)
            tc_elements = tr.findall(qn("w:tc"))

            for tc in tc_elements:
                grid_span = _get_grid_span(tc)
                v_merge = _get_vmerge(tc)
                covered_columns = range(
                    column_index,
                    column_index + grid_span,
                )

                if v_merge == "continue":
                    merge_cell = None
                    for column in covered_columns:
                        if column in active_vertical_merges:
                            merge_cell = active_vertical_merges[column]
                            break

                    if merge_cell is not None:
                        merge_cell["row_span"] += 1

                    column_index += grid_span
                    continue

                cell_data = {
                    "row": row_index,
                    "column": column_index,
                    "row_span": 1,
                    "col_span": grid_span,
                    "text": _get_cell_text(tc),
                }

                cells.append(cell_data)

                if v_merge == "restart":
                    for column in covered_columns:
                        active_vertical_merges[column] = cell_data
                else:
                    for column in covered_columns:
                        active_vertical_merges.pop(column, None)

                column_index += grid_span

            rows.append(
                {
                    "row": row_index,
                    "cells": cells,
                }
            )

        result["tables"].append(
            {
                "table_index": table_index,
                "rows": rows,
            }
        )

    return result


def parse_directory(
    input_dir: str | Path,
    output_dir: str | Path,
):
    """
    Рекурсивно обрабатывает все DOCX-файлы.
    """
    input_dir = Path(input_dir)
    output_dir = Path(output_dir)

    if not input_dir.exists():
        raise FileNotFoundError(f"Каталог ввода не существует: {input_dir}")

    if not input_dir.is_dir():
        raise NotADirectoryError(f"Путь ввода не является каталогом: {input_dir}")

    docx_files = sorted(input_dir.rglob("*.docx"))

    print(f"Found {len(docx_files)} DOCX files")

    for file_path in docx_files:
        relative_path = file_path.relative_to(input_dir)
        output_path = (output_dir / relative_path).with_suffix(".json")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        data = parse_docx(file_path)

        import json

        with output_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        print(f"OK: {file_path} -> {output_path}")


if __name__ == "__main__":
    parse_directory(
        input_dir=("/home/n0mad/Projects/Meditron data parsing test/protocols"),
        output_dir=("/home/n0mad/Projects/Meditron data parsing test/output"),
    )
