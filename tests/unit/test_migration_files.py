"""Защита от повторных ревизий и ошибочного копирования экранированных путей Git."""

import ast
from pathlib import Path


def test_revision_files_are_unique():
    """Для каждой ревизии существует один обычный файл, кириллица допустима."""
    directory = Path(__file__).resolve().parents[2] / "alembic" / "versions"
    revisions = []
    for path in directory.iterdir():
        if path.is_dir():
            continue
        assert '"' not in path.name and "\\" not in path.name, f"Некорректное имя: {path.name}"
        if path.suffix != ".py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "revision":
                revisions.append(ast.literal_eval(node.value))
    assert len(revisions) == len(set(revisions)), "Обнаружены повторные ревизии Alembic"
    assert revisions.count("e18a439f9a84") == 1, "Начальная миграция должна быть единственной"
