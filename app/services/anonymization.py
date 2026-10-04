"""Консервативное обезличивание: заменяем распознанные данные до извлечения.

ФИО пациента, дата рождения, телефон, адрес и номер карты заменяются
пробелами той же длины:
клинические цитаты и абсолютные смещения остаются дословными. Эвристики не
гарантируют распознавание произвольных персональных данных; оператор обязан
проверить весь текст до подтверждения.
ФИО врача не обезличивается по решению заказчика. Дата приёма сохраняется:
она нужна для маршрута и SLA. Клинические данные (пол, возраст, диагнозы,
размеры органов, BI-RADS) сохраняются: их удаление ломает цитаты и объяснимость.
Исходник и имя файла не сохраняются.
"""

import re
from io import BytesIO
from typing import TypedDict
from xml.etree import ElementTree
from zipfile import ZipFile

SPACE = r"[^\S\r\n]"
AGE_LABEL = rf"Возраст(?:{SPACE}+пациента)?(?:{SPACE}+на{SPACE}+момент{SPACE}+осмотра)?"

# Следующая метка завершает значение даже после снятия тегов Word.
FIELD_END = (
    r"(?=[ \t]*(?:\b(?:Ф\.?И\.?О\.?(?: пациента)?|Пациент(?:ка)?"
    r"|Дата рождения|Дата при[её]ма|Время|Врач|Адрес(?: проживания| регистрации)?"
    r"|Место жительства|Телефон|Тел\.?|phone|Номер(?: амбулаторной)? карты|Пол"
    r"|Возраст|Диагноз|Заключение|Размеры(?: органов)?|BI-RADS)[ \t]*:"
    r"|\bBI-RADS\b)|[\r\n;]|$)"
)


# Известные метки дают точную границу даже при произвольном регистре слов.
KNOWN_LABEL = (
    r"(?:Ф\.?И\.?О\.?(?:[^\S\r\n]+пациента)?|Пациент(?:ка)?|Фамилия|Имя|Отчество"
    r"|Дата[^\S\r\n]+(?:рождения|при[её]ма)|Д\.[^\S\r\n]*р\.|Рожд[её]н[а]?"
    r"|Время|Врач|Адрес|Место[^\S\r\n]+жительства|Телефон|Тел\.|phone"
    r"|Номер[^\S\r\n]+(?:амбулаторной[^\S\r\n]+)?карты"
    rf"|{AGE_LABEL}|Пол|Диагноз|Заключение|Размеры|BI[- ]?RADS|O[- ]?RADS|Код)"
    r"(?:[^\S\r\n]+[\w/-]+)*[^\S\r\n]*:"
)
# Произвольная метка дополняет справочник. Не включаем в неё значение,
# стоящее перед известной меткой (например, имя пациента перед возрастом).
LABEL_END = (
    rf"(?=[^\S\r\n]*(?<![^\W\d_])(?:{KNOWN_LABEL}|"
    rf"(?![^:;\r\n]*[^\S\r\n]+{KNOWN_LABEL})"
    r"[^\W\d_][\w/-]*(?:[^\S\r\n]+[\w/-]+)*[^\S\r\n]*:))"
)
SENTENCE_END = r"(?<!ул)(?<!УЛ)(?<!Ул)(?<!д)(?<!кв)(?=[.!?](?:[^\S\r\n]+|$)|[\r\n;]|$)"


def field(label: str) -> str:
    """Граница слова допускает метку в середине абзаца; значение не съедает соседнее поле."""
    label = label.replace(r"[ \t]", SPACE).replace(" ", SPACE + "+")
    # Проверяем общую границу перед каждым символом значения.
    # Справочник остаётся дополнительной границей, а не условием её поиска.
    boundary = rf"(?:{LABEL_END}|{SENTENCE_END}|{FIELD_END})"
    return (
        rf"(?im)\b(?:{label})[^\S\r\n]*[:\-]\s*"
        rf"((?:(?!{boundary})[^\r\n;])*)"
    )


PATTERNS = (
    ("ФИО", field(r"Ф\.?И\.?О\.?(?: пациента)?|Пациент(?:ка)?|Фамилия|Имя|Отчество")),
    ("Адрес", field(r"Адрес(?: проживания| регистрации)?|Место жительства")),
    ("Дата рождения", field(r"Дата рождения|Д\.?[ \t]*р\.?|Рожд[её]н[а]?")),
    ("Телефон", field(r"Телефон|Тел\.?|phone")),
    ("Номер карты", field(r"Номер амбулаторной карты|Номер карты")),
    (
        "Номер карты",
        rf"(?im)\bКарта[^\S\r\n]*№[^\S\r\n]*([^\r\n]*?)(?:{LABEL_END}|{SENTENCE_END}|(?=[^\S\r\n]*Протокол\b))",
    ),
    ("ФИО", r"\b[А-ЯЁ][а-яё]+[ \t]+[А-ЯЁ][а-яё]+[ \t]+[А-ЯЁ][а-яё]+(?:вич|вна)\b"),
    ("ФИО", r"\b[А-ЯЁ][а-яё]+[ \t]+[А-ЯЁ]\.[ \t]*[А-ЯЁ]\."),
    ("ФИО", r"(?i)\b[а-яё]+[ \t]+[а-яё]+[ \t]+[а-яё]+(?:вич|вна)\b"),
    ("Телефон", r"(?<!\w)(?:\+7|8)[ (\-]*\d{3}[ )\-]*\d{3}[ \-]*\d{2}[ \-]*\d{2}(?!\d)"),
    ("Телефон", r"(?<!\w)\+?\d(?:[ ()-]*\d){9,14}(?!\w)"),
)


class Change(TypedDict):
    kind: str
    original: str
    start: int
    end: int
    replacement: str


def read_docx(raw: bytes) -> str:
    """Читаем абзацы и ячейки в порядке XML, не сохраняя исходный файл."""
    with ZipFile(BytesIO(raw)) as archive:
        xml = archive.read("word/document.xml")
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    parts: list[str] = []
    # События конца элемента ставят границы после текста абзаца/ячейки.
    for _, node in ElementTree.iterparse(BytesIO(xml), events=("end",)):
        if node.tag == namespace + "t":
            parts.append(node.text or "")
        elif node.tag == namespace + "tab":
            parts.append("\t")
        elif node.tag in {namespace + "br", namespace + "cr", namespace + "p", namespace + "tc"}:
            parts.append("\n")
    return "".join(parts)


def anonymize_document(raw: bytes) -> tuple[str, list[Change]]:
    """Обезличиваем DOCX в памяти; смещения относятся к извлечённому тексту."""
    return anonymize(read_docx(raw))


def anonymize(text: str) -> tuple[str, list[Change]]:
    """Маскируем данные пациента, сохраняя длину текста и смещения цитат."""
    doctors = [match.span(1) for match in re.finditer(field("Врач"), text)]
    # Клинические значения защищены и от общих эвристик ФИО/телефонов.
    clinical = [
        match.span()
        for match in re.finditer(
            field(
                r"Возраст(?:[^:;\r\n]*?)|Пол(?:[^:;\r\n]*?)|Диагноз(?:[^:;\r\n]*?)"
                r"|Заключение(?:[^:;\r\n]*?)|Размеры(?:[^:;\r\n]*?)"
                r"|BI[- ]?RADS|O[- ]?RADS|Код(?:[^:;\r\n]*?)"
            ),
            text,
        )
    ]
    spans: list[Change] = []
    covered: set[int] = set()
    for kind, pattern in PATTERNS:
        for match in re.finditer(pattern, text):
            start, end = match.span(1) if match.lastindex else match.span()
            if start == end or any(
                start < right and end > left for left, right in doctors + clinical
            ):
                continue
            if all(i in covered for i in range(start, end)):
                continue
            covered.update(range(start, end))
            spans.append(
                {
                    "kind": kind,
                    "original": text[start:end],
                    "start": start,
                    "end": end,
                    "replacement": "Пробелы той же длины",
                }
            )
    chars = list(text)
    for i in covered:
        if chars[i] not in "\r\n":
            chars[i] = " "
    return "".join(chars), sorted(spans, key=lambda s: s["start"])
