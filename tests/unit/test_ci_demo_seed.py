"""Проверка демо-сида использует количество исходных файлов и отвергает расхождения."""

from unittest.mock import AsyncMock

import pytest

from scripts import check_demo_seed


@pytest.mark.parametrize("counts, expected_code", [([3, 3], 0), ([2], 1), ([3, 4], 1)])
async def test_seed_counts_come_from_files(tmp_path, monkeypatch, capsys, counts, expected_code):
    """Недостающие и лишние записи дают ненулевой код без фиксированного размера набора."""
    for name in ("first", "second", "third"):
        (tmp_path / f"{name}.txt").touch()
    monkeypatch.setattr(check_demo_seed, "PROTOCOLS_DIR", tmp_path)
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.scalar.side_effect = counts
    monkeypatch.setattr(check_demo_seed, "SessionFactory", lambda: session)
    monkeypatch.setattr(check_demo_seed, "engine", AsyncMock())
    assert await check_demo_seed.main() == expected_code
    output = capsys.readouterr().out
    if expected_code:
        assert "ожидается 3 записей по исходным файлам" in output
        assert f"найдено {counts[-1]}" in output
