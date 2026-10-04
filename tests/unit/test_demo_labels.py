"""Контроль опубликованной разметки и соответствия демо-протоколам."""

import shutil

import pytest

from app.services.quality import load_labeled_samples
from scripts.generate_demo_labels import (
    LABELS_DIR,
    PROTOCOLS_DIR,
    serialize_labels,
    verify_protocol_coverage,
)


def test_published_labels_are_deterministic():
    """Две генерации совпадают побайтово с артефактом поставки."""
    published = (LABELS_DIR / "labeled.json").read_bytes()
    assert serialize_labels() == published
    assert serialize_labels() == published


def test_protocol_coverage():
    """Все файлы охвачены метками или явным исключением и не изменены."""
    verify_protocol_coverage(load_labeled_samples(LABELS_DIR), PROTOCOLS_DIR)


@pytest.mark.parametrize("mutation", ["add", "delete", "change", "remove_label", "unknown_id"])
def test_desynchronization_is_rejected(tmp_path, mutation):
    """Добавление файла, потеря метки и изменения данных блокируют оценку."""
    directory = tmp_path / "protocols"
    shutil.copytree(PROTOCOLS_DIR, directory)
    samples = load_labeled_samples(LABELS_DIR)
    path = next(directory.glob("*.txt"))
    if mutation == "add":
        (directory / "demo_new_01.txt").write_text("Новый протокол", encoding="utf-8")
    elif mutation == "delete":
        path.unlink()
    elif mutation == "change":
        path.write_text("Изменённый протокол", encoding="utf-8")
    elif mutation == "remove_label":
        samples.pop()
    else:
        from dataclasses import replace

        samples[0] = replace(samples[0], study_id="demo_unknown_01")
    with pytest.raises(ValueError, match="Рассинхрон"):
        verify_protocol_coverage(samples, directory)
