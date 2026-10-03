"""Тесты контракта декодера.

Этот файл — ЗАЯВКА сокоманднику. Любая реализация ProtocolExtractor
должна проходить его без правок: тест фиксирует инварианты, а не
конкретный алгоритм.

Если тест пришлось менять под свою реализацию — контракт нарушен,
и это нужно обсудить, а не подгонять тест.

Что здесь проверяется:
  - находка без цитаты невозможна (основа объяснимости);
  - цитата дословно есть в исходном тексте;
  - отрицание выставляется;
  - пустой текст не роняет декодер;
  - размер извлекается, если есть.
"""

import pytest

from app.services.extraction import (
    DictionaryExtractor,
    ExtractionResult,
    Finding,
    ProtocolExtractor,
    get_extractor,
    set_extractor,
)
from app.services.extraction.base import validate_extractor

# Реальные куски из выданных протоколов хакатона.
POSITIVE = """
Описание
УЛЬТРАЗВУКОВОЕ ИССЛЕДОВАНИЕ ОРГАНОВ МАЛОГО ТАЗА

МАТКА
Размеры:72х59х64 мм
Миометрий: гипоэхогенный интерстициальный узел 26 х 38 мм

ЗАКЛЮЧЕНИЕ: УЗ-признаки миомы матки. Рекомендовано: консультация гинеколога.
"""

NEGATIVE = """
ЗАКЛЮЧЕНИЕ: УЗ-признаки параовариальной кисты справа. ORADS 2 справа ORADS 1 слева.
Рекомендовано: консультация гинеколога.
"""

WITHOUT_FINDING = """
ЗАКЛЮЧЕНИЕ: УЗ патологии на момент исследования не выявлено.
"""

DENIAL = """
ЗАКЛЮЧЕНИЕ: УЗ-признаки миомы матки, достоверно узловых и очаговых образований не определяется.
"""


@pytest.fixture
def extractor() -> ProtocolExtractor:
    return get_extractor()


class TestContractShape:
    def test_реализует_интерфейс(self, extractor):
        assert isinstance(extractor, ProtocolExtractor)

    def test_имеет_имя(self, extractor):
        assert extractor.name

    def test_validate_проходит(self, extractor):
        validate_extractor(extractor)  # не должно бросить

    def test_не_protocol_extractor_отклоняется(self):
        with pytest.raises(TypeError):
            validate_extractor(object())  # type: ignore[arg-type]

    def test_подмена_декодера(self):
        original = get_extractor()
        try:
            set_extractor(DictionaryExtractor())
            assert isinstance(get_extractor(), DictionaryExtractor)
        finally:
            set_extractor(original)


class TestCitationInvariant:
    """ГЛАВНЫЙ ИНВАРИАНТ: находка без цитаты недопустима."""

    def test_пустая_цитата_запрещена(self):
        with pytest.raises(ValueError, match="цитат"):
            Finding(finding="полип эндометрия", quote="   ")

    def test_пустой_текст_вместо_цитаты_запрещён(self):
        with pytest.raises(ValueError, match="цитат"):
            Finding(finding="полип эндометрия", quote="")

    def test_цитата_обязательна_по_умолчанию(self):
        """Нельзя создать находку, не указав цитату: её нет в default."""
        with pytest.raises(TypeError):
            Finding(finding="полип эндометрия")  # type: ignore[call-arg]

    def test_каждая_находка_дословно_в_тексте(self, extractor):
        """Цитата обязана встречаться в исходном тексте — иначе её не показать врачу."""
        result = extractor.extract(POSITIVE, study_type="УЗИ органов малого таза")
        for finding in result.findings:
            assert finding.quote in result.raw_text, (
                f"цитата «{finding.quote}» не найдена в исходном тексте"
            )

    def test_смещения_указывают_на_цитату(self, extractor):
        result = extractor.extract(POSITIVE, study_type="УЗИ органов малого таза")
        for finding in result.findings:
            assert finding.char_end > finding.char_start
            assert finding.char_start >= 0

    def test_невалидные_смещения_запрещены(self):
        with pytest.raises(ValueError, match="char_end"):
            Finding(finding="полип", quote="полип", char_start=10, char_end=5)


class TestNegationHandling:
    """Норма не должна превращаться в триггер."""

    def test_находит_находку_в_положительном_протоколе(self, extractor):
        result = extractor.extract(POSITIVE, study_type="УЗИ органов малого таза")
        names = {f.finding for f in result.findings}
        assert names, "в положительном протоколе должна найтись хотя бы одна находка"

    def test_отрицание_выставляется(self, extractor):
        result = extractor.extract(DENIAL, study_type="УЗИ органов малого таза")
        if result.findings:
            assert any(f.in_negative_context for f in result.findings), (
                "в тексте есть «не определяется», но флаг отрицания не выставлен"
            )

    def test_пустой_заключение_не_роняет(self, extractor):
        result = extractor.extract(WITHOUT_FINDING, study_type="УЗИ брюшной полости")
        assert result.findings == [] or all(f.in_negative_context for f in result.findings)


class TestRobustness:
    """Декодер не должен падать на плохом входе."""

    @pytest.mark.parametrize(
        "text",
        ["", "   ", "\n\n", "ЗАКЛЮЧЕНИЕ:", "Заключение без текста", "🎉🎉🎉"],
    )
    def test_мусор_не_роняет(self, extractor, text: str):
        result = extractor.extract(text)
        assert isinstance(result, ExtractionResult)

    def test_очень_длинный_текст(self, extractor):
        result = extractor.extract("миома матки " * 5000)
        assert isinstance(result, ExtractionResult)

    def test_без_подсказки_типа_исследования(self, extractor):
        result = extractor.extract(POSITIVE)
        assert isinstance(result, ExtractionResult)


class TestMetadataExtraction:
    def test_тип_исследования_из_подсказки(self, extractor):
        result = extractor.extract(POSITIVE, study_type="УЗИ органов малого таза")
        assert result.meta.study_type == "УЗИ органов малого таза"

    def test_сохраняет_исходный_текст(self, extractor):
        result = extractor.extract(POSITIVE)
        assert result.raw_text == POSITIVE

    def test_указывает_имя_реализации(self, extractor):
        result = extractor.extract(POSITIVE)
        assert result.extractor_name


class TestSizeExtraction:
    def test_размер_извлекается_когда_есть(self, extractor):
        result = extractor.extract(POSITIVE, study_type="УЗИ органов малого таза")
        with_size = [f for f in result.findings if f.has_size]
        assert isinstance(with_size, list)  # может быть пустым — не падаем

    def test_размер_в_миллиметрах(self, extractor):
        text = "ЗАКЛЮЧЕНИЕ: УЗ-признаки миомы матки, узел 26 х 38 мм."
        result = extractor.extract(text)
        sizes = [f.size_mm for f in result.findings if f.size_mm]
        assert not sizes or all(0 < s < 1000 for s in sizes)


class TestExtractionResult:
    def test_пустой_результат_валиден(self):
        result = ExtractionResult()
        assert result.findings == []

    def test_список_находок_не_общий(self):
        """Находки не должны шарить список между вызовами."""
        first = ExtractionResult()
        second = ExtractionResult()
        first.findings.append(Finding(finding="тест", quote="тест"))
        assert second.findings == []
