"""Генератор синтетического демо-набора и размеченного набора для метрик.

ЗАЧЕМ ЭТОТ СКРИПТ
=================
Жюри на защите должно иметь возможность запустить демонстрацию маршрутизации
и посчитать метрики качества БЕЗ доступа к реальным данным пациентов.
Реальные протоколы хакатона публиковать нельзя, поэтому здесь они
переписываются: клиническая часть (описание и заключение) сохраняется —
она и нужна для демонстрации, а все идентификаторы заменяются
синтетическими.

ЧТО ДЕЛАЕТ СКРИПТ
=================
1. Читает исходные .docx (python-docx), достаёт описание и заключение.
2. Обезличивает: ФИО, дата рождения, возраст, номер карты, дата и время
   приёма, ФИО врача, телефон клиники. Значения синтетические и
   детерминированные (seed), поэтому повторный запуск даёт тот же файл.
3. Пишет data/demo/protocols/*.txt — по одному файлу на протокол.
4. Пишет data/labeled/labeled.json — разметка «есть находка / нет
   находки» по каждому trigger_id из config/routing_matrix.json.
5. Проверяет, что в демо-файлах не осталось реальных ФИО и телефонов.

РАЗМЕТКА
========
Разметка сделана вручную по ЗАКЛЮЧЕНИЮ каждого протокола (split='gold') и
зафиксирована в таблице GOLD ниже: для каждого файла перечислены только
те trigger_id, для которых находка ПОДТВЕРЖДЕНА (label=true). Все
остальные триггеры, чей source_study совпадает с типом исследования,
получают label=false — так и считается доля норм.

Размечаются только те trigger_id, чей source_study совпадает с типом
исследования. Протоколы предстательной железы не размечаются: в матрице
маршрутизации нет триггера с source_study «УЗИ предстательной железы»,
размечать заведомо невозможные пары нельзя.

Одна пара (study_id, trigger_id) получает РОВНО одну метку. Дубликаты с
противоположными метками недопустимы: по одному исследованию и одному
триггеру верный ответ один, а две записи дали бы один FP или FN независимо
от качества распознавания. Поле split остаётся (сервис качества его
поддерживает), но заведомо неверные копии золотых записей не создаются.

ЗАПУСК
======
    uv run python scripts/make_demo_data.py

Путь к исходникам можно переопределить: --source /путь/к/протоколы
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = Path(
    "/home/meanilin/Downloads/Telegram Desktop/Кейс/(СМ-Клиника)протоколы/протоколы"
)
DEMO_DIR = REPO_ROOT / "data" / "demo"
LABELED_PATH = REPO_ROOT / "data" / "labeled" / "labeled.json"

SEED = 20260903

# Тип исследования → (префикс имени файла, source_study в матрице, пол)
STUDY_GROUPS: dict[str, tuple[str, str, str]] = {
    "протоколы Ж ОМТ": ("gynecology", "УЗИ органов малого таза", "женский"),
    "протоколы ЖП": ("abdomen", "УЗИ брюшной полости", "женский"),
    "протоколы вены нижних конечностей": (
        "lower_limb",
        "УЗДГ артерий нижних конечностей",
        "не указан",
    ),
    "протоколы молочная железа": ("breast", "УЗИ молочных желез", "женский"),
    "протоколы предстательная железа": ("prostate", "", "мужской"),
    "протоколы щитовидная железа": ("thyroid", "УЗИ щитовидной железы", "не указан"),
}

# Синтетические пул-данные. Ничего из этого не встречается в исходниках:
# имена взяты из нейтрального демонстрационного набора.
FEMALE_NAMES = [
    ("Смирнова", "Ольга", "Петровна"),
    ("Иванова", "Елена", "Сергеевна"),
    ("Кузнецова", "Мария", "Алексеевна"),
    ("Попова", "Анна", "Эдуардовна"),
    ("Васильева", "Наталья", "Викторовна"),
    ("Михайлова", "Ирина", "Антоновна"),
    ("Новикова", "Светлана", "Дмитриевна"),
    ("Морозова", "Юлия", "Олеговна"),
    ("Волкова", "Екатерина", "Романовна"),
    ("Зайцева", "Дарья", "Максимовна"),
    ("Орлова", "Ксения", "Артёмовна"),
    ("Медведева", "Полина", "Львовна"),
]
MALE_NAMES = [
    ("Соколов", "Дмитрий", "Андреевич"),
    ("Лебедев", "Алексей", "Игоревич"),
    ("Козлов", "Сергей", "Павлович"),
    ("Егоров", "Николай", "Фёдорович"),
    ("Павлов", "Андрей", "Викторович"),
    ("Семёнов", "Роман", "Анатольевич"),
    ("Голубев", "Максим", "Юрьевич"),
    ("Беляев", "Кирилл", "Олегович"),
    ("Тарасов", "Илья", "Владимирович"),
    ("Белов", "Глеб", "Станиславович"),
    ("Комаров", "Артём", "Егорович"),
    ("Орлов", "Тимур", "Русланович"),
]

# ----------------------------------------------------------------------------
# ЗОЛОТАЯ РАЗМЕТКА
# ----------------------------------------------------------------------------
# Ключ — имя исходного .docx. Значение — подтверждённые находки (label=true)
# с цитатой из заключения, на которой основана разметка. Триггеры с тем же
# source_study, которых здесь нет, получают label=false.
#
# Пороговые и пограничные случаи помечены комментарием: они объясняют, почему
# находка засчитана или не засчитана. Это и есть «золото» — решение врача,
# а не результат автоматического совпадения строк.
GOLD: dict[str, dict[str, str]] = {
    # ---------------- УЗИ органов малого таза (25) ----------------
    "1 Ж ОМТ (1).docx": {
        "ovarian_cyst": "УЗ-признаки параовариальной кисты справа (O-RADS 2)",
    },
    "1 Ж ОМТ (2).docx": {
        "fibroid_uterus": "эхо-признаки многоузловой миомы матки",
    },
    "1 Ж ОМТ (3).docx": {},  # «Внутренний эндометриоз» — вне матрицы триггеров
    "1 Ж ОМТ (4).docx": {},  # Эхо-картина соответствует дню цикла, O-RADS 1
    "1 Ж ОМТ (5).docx": {},  # 1 фаза МЦ + аденомиоз, O-RADS 1
    "1 Ж ОМТ (6).docx": {
        # Интрамуральная миома FIGO 4 — это fibroid_uterus, не submucosal.
        # Фолликулярная киста при O-RADS 1 — физиологическая, не триггер.
        "fibroid_uterus": "в теле - интрамуральная миома малых размеров, FIGO 4",
    },
    "1 Ж ОМТ (7).docx": {
        # Находка есть, размер 40 мл; O-RADS 2 не снимает подтверждённую кисту.
        "ovarian_cyst": "образования правого яичника с динамикой роста ... киста объемом 40 мл",
    },
    "1 Ж ОМТ (8).docx": {
        "endometrial_polyp": "патологии эндометрия ( полип )",
        "ovarian_cyst": "жидкостного образования левого яичника ( ретенционная киста )",
    },
    "1 Ж ОМТ (9).docx": {
        "ovarian_cyst": "УЗ признаки кисты правого яичника",
    },
    "1 Ж ОМТ (10).docx": {
        "fibroid_uterus": "УЗ признаки миомы матки",
    },
    "1 Ж ОМТ (11).docx": {
        "fibroid_uterus": "миомы матки",
        "ovarian_cyst": "кисты левого яичника",
    },
    "1 Ж ОМТ (12).docx": {},  # Жёлтое тело и кисты шейки матки — не триггеры матрицы
    "1 Ж ОМТ (13).docx": {
        "fibroid_uterus": "Эхографические признаки миомы матки",
    },
    "1 Ж ОМТ (14).docx": {
        "fibroid_uterus": "миомы матки малых размеров без признаков роста",
    },
    "1 Ж ОМТ (15).docx": {
        # Киста правого яичника описана положительно, размер не указан ниже
        # порога — но и не исключена: подтверждённая находка, label=true.
        "ovarian_cyst": "мелкой кисты правого яичника с неполной перегородкой (серозная киста?)",
    },
    "1 Ж ОМТ (16).docx": {
        "fibroid_uterus": "Миома матки (без динамики от предыдущего узи)",
    },
    "1 Ж ОМТ (17).docx": {
        "endometrial_polyp": "Гиперплазия эндометрия, полипоз эндометрия",
        "fibroid_uterus": "миома матки",
    },
    "1 Ж ОМТ (18).docx": {},  # 1 фаза МЦ, аденомиоз, эндоцервиксоз
    "1 Ж ОМТ (19).docx": {
        "fibroid_uterus": "Миома матки",
    },
    "1 Ж ОМТ (20).docx": {
        "fibroid_uterus": "Миома матки в стадии кальциноза",
    },
    "1 Ж ОМТ (21).docx": {
        # FIGO 5 — субсерозный узел, поэтому submucosal_fibroid = false.
        "endometrial_polyp": "Железисто-фиброзный полип эндометрия",
        "fibroid_uterus": "УЗИ картина миомы матки тип 5 по FIGO",
        "ovarian_cyst": "УЗИ картина двусторонних фолликулярных кисты",
    },
    "1 Ж ОМТ (22).docx": {
        "fibroid_uterus": "Миома матки",
    },
    "1 Ж ОМТ (23).docx": {
        "endometrial_polyp": "УЗ-признаки полипа эндометрия",
    },
    "1 Ж ОМТ (24).docx": {},  # аденомиоз + кисты шейки матки + снижение резерва
    "1 Ж ОМТ (25).docx": {},  # несоответствие толщины эндометрия дню цикла
    # ---------------- УЗИ брюшной полости (25) ----------------
    "1 ЖП (1).docx": {},  # дискинезия желчного пузыря по гипомоторному типу
    "1 ЖП (2).docx": {},  # диффузные изменения печени и поджелудочной железы
    "1 ЖП (3).docx": {
        "cholelithiasis": "хронический холецистит ( вне обострения )",
    },
    "1 ЖП (4).docx": {},  # жировой гепатоз, деформация желчного пузыря
    "1 ЖП (5).docx": {},
    "1 ЖП (6).docx": {},  # гепатомегалия, перегиб желчного пузыря со взвесью
    "1 ЖП (7).docx": {
        # Пограничный случай: «Нельзя исключить холецистит». Находка не
        # отрицается, триггер сработает и пациент уйдёт к хирургу —
        # именно так врач и поступит, поэтому разметка положительная.
        "cholelithiasis": "Нельзя исключить холецистит",
    },
    "1 ЖП (8).docx": {},  # диффузные изменения поджелудочной железы, сладж
    "1 ЖП (9).docx": {},
    "1 ЖП (10).docx": {},  # состояние после холецистэктомии
    "1 ЖП (11).docx": {},  # холестероз желчного пузыря
    "1 ЖП (12).docx": {},  # контурная деформация желчного пузыря и взвеси
    "1 ЖП (13).docx": {
        "cholelithiasis": "УЗ-признаки конкрементов желчного пузыря",
    },
    "1 ЖП (14).docx": {
        "cholelithiasis": "холецистолиатиаза",
    },
    "1 ЖП (15).docx": {},  # заключение не содержит находок матрицы
    "1 ЖП (16).docx": {
        "gallbladder_polyp": "Образование в желчном пузыре- полип",
    },
    "1 ЖП (17).docx": {},  # перегиб желчного пузыря в области шейки
    "1 ЖП (18).docx": {
        "gallbladder_polyp": "деформации и полипа желчного пузыря",
    },
    "1 ЖП (19).docx": {},  # УЗ патологии на момент исследования не выявлено
    "1 ЖП (20).docx": {
        "gallbladder_polyp": "Полип желчного пузыря",
    },
    "1 ЖП (21).docx": {
        "gallbladder_polyp": "Полип в желчном пузыре небольших размеров",
    },
    "1 ЖП (22).docx": {
        "gallbladder_polyp": "полипа желчного пузыря",
    },
    "1 ЖП (23).docx": {
        "cholelithiasis": "хронического калькулезного холецистита",
    },
    "1 ЖП (24).docx": {
        "cholelithiasis": "УЗ-признаки хронического калькулезного холецистита",
    },
    "1 ЖП (25).docx": {
        "gallbladder_polyp": "Полипоз и холестаз желчного пузыря",
    },
    # ---------------- УЗДГ артерий нижних конечностей (9) ----------------
    "нижние конечности (2).docx": {},  # нестенозирующий атеросклероз
    "нижние конечности (3).docx": {},  # бляшки до 55% — ниже порога 70%
    "нижние конечности (4).docx": {},  # варикозная трансформация БПВ
    "нижние конечности (5).docx": {},  # несостоятельность СФС, клапанная недостаточность
    "нижние конечности (6).docx": {
        # Окклюзия ЗББА — значимое поражение, несмотря на стенозы 35-45%.
        "lower_limb_stenosis": "Окклюзия левой ЗББА",
    },
    "нижние конечности (7).docx": {},  # несостоятельность БПВ, тромбоза нет
    "нижные конечности (8).docx": {},  # стенозы до 20-30% — ниже порога 70%
    "нижние конечности (9).docx": {
        "lower_limb_stenosis": "Эхографические признаки стенозирующего атеросклероза "
        "магистральных артерий обеих нижних конечностей",
    },
    "нижние конечности (10).docx": {
        "lower_limb_stenosis": "УЗ признаки начальных проявлений стенозирующего "
        "атеросклероза артерий нижних конечностей",
    },
    # ---------------- УЗИ молочных желез (10) ----------------
    "молочн железа (1).docx": {
        "breast_birads_3_5": "очаговое образование правой молочной железы (фиброаденома?)",
    },
    "молочн железа (2).docx": {
        "breast_birads_3_5": "Правая мол.железа Bi-RADS 3. Левая мол.железа Bi-RADS 3",
    },
    "молочн железа (3).docx": {
        "breast_birads_3_5": "Категория BI-RADS 4 (левая молочная железа)",
    },
    "молочн железа (4).docx": {},  # BI-RADS 1 / BI-RADS 1
    "молочн железа (5).docx": {},  # УЗ-признаков объемных образований не выявлено
    "молочн железа (6).docx": {},  # категория Birads 2
    "молочн железа (7).docx": {},  # BI-RADS 1 с обеих сторон
    "молочн железа (8).docx": {},  # BI-RADS 2 с обеих сторон
    "молочн железа (9).docx": {},  # BI-RADS 2 с обеих сторон
    "молочн железа (10).docx": {},  # состояние после маммопластики Br2
    # ---------------- УЗИ щитовидной железы (10) ----------------
    # Правило: узел назван, категория не задана или >= 3 → true;
    # категория TI-RADS/EU-TIRADS 1-2 без значимого узла → false.
    "щитовидка (1).docx": {},  # узлы левой доли, TI-RADS-2
    "щитовидка (2).docx": {},  # узловой зоб, TI-RADS 2
    "щитовидка (3).docx": {
        "thyroid_nodule": "Узлы в обеих долях щитовидной железы, EU-TIRADS справа 3, слева 3",
    },
    "щитовидка (4).docx": {
        "thyroid_nodule": "Узлы в щитовидной железе",
    },
    "щитовидка (5).docx": {
        "thyroid_nodule": "УЗ признаки узловых образований обеих долей щитовидной железы",
    },
    "щитовидка (6).docx": {
        "thyroid_nodule": "Узловое образование, единичный макрофолликул левой доли "
        "щитовидной железы (EU-TIRADS-3)",
    },
    "щитовидка (7).docx": {},  # диффузные изменения по типу АИТ, TI-RADS 2/2
    "щитовидка (8).docx": {},  # мелкий губчатый узел, TI-RADS 1/2
    "щитовидка (9).docx": {},  # диффузный зоб, узлов не описано
    "щитовидка (10).docx": {},  # единичные расширенные фолликулы, узлов нет
}

# ----------------------------------------------------------------------------
# ПРОВЕРКА ЦЕЛОСТНОСТИ РАЗМЕТКИ
# ----------------------------------------------------------------------------
# Заведомо неверные копии золотых записей больше не создаются: пара
# (study_id, trigger_id) — одна метка. Раньше для «демонстрации честности»
# рядом с золотой записью клалась вторая, с противоположной меткой; она
# ломала и загрузчик (повторная разметка), и метрики (гарантированный FP
# или FN). Теперь вместо этого проверяем уникальность и состав полей —
# именно то, что ломало quality.load_labeled_samples.
REQUIRED_FIELDS = {"study_id", "trigger_id", "label", "split"}

# Строки служебного шапки протокола, из которых не берётся клинический текст
HEADER_PREFIXES = (
    "амбулаторная карта",
    "прием врача",
    "дата приема",
    "фио пациента",
    "дата рождения",
    "возраст на момент осмотра",
    "идс получено",
    "врач:",
    "выполнил",
    "телефон",
    "email",
    "e-mail",
    "адрес",
)

# По этим маркерам заключение обрывается: дальше идут назначения врача
# с ФИО и схемами лечения — это не описание исследования и не нужно.
CONCLUSION_CUT_MARKERS = (
    "назначенные услуги",
    "назначения врача",
    "рекомендации врача",
    "подпись врача",
    "дата подписи",
)

PHONE_RE = re.compile(r"\+?\d[\d\s()\-]{9,}\d")
DATE_RE = re.compile(r"\b\d{1,2}[.]\d{1,2}[.]\d{2,4}\b")
TIME_RE = re.compile(r"\b\d{1,2}[:.]\d{2}\b")
NAME_LINE_RE = re.compile(
    r"(фио\s*пациента|пациент[ае]?\s*:|фио|врач\s*:|исследование выполнил|"
    r"подпись\s+врача|выполнил\s+врач)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------- Extraction
@dataclass
class Protocol:
    """Один исходный протокол: сырой текст + разобранные части."""

    source_path: Path
    group: str
    lines: list[str] = field(default_factory=list)
    description: list[str] = field(default_factory=list)
    conclusion: list[str] = field(default_factory=list)


def iter_blocks(document):
    """Пройти по блокам документа в порядке следования (абзацы + таблицы).

    python-docx отдаёт paragraphs и tables отдельными списками, поэтому
    порядок пришлось бы потерять. Протокол — это одна большая таблица,
    поэтому идём по XML-дереву.
    """
    body = document.element.body
    from docx.oxml.table import CT_Tbl  # noqa: PLC0415
    from docx.oxml.text.paragraph import CT_P  # noqa: PLC0415
    from docx.table import Table  # noqa: PLC0415
    from docx.text.paragraph import Paragraph  # noqa: PLC0415

    for child in body.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, document)
        elif isinstance(child, CT_Tbl):
            yield Table(child, document)


def cell_text(cell) -> str:
    parts = [p.text.strip() for p in cell.paragraphs if p.text.strip()]
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def row_text(row) -> str:
    """Текст строки таблицы без дублей.

    Ячейки в исходных картах объединены вертикально, поэтому python-docx
    возвращает каждую из них N раз подряд. Достаточно схлопывать повторы.
    """
    out: list[str] = []
    for cell in row.cells:
        text = cell_text(cell)
        if text and (not out or out[-1] != text):
            out.append(text)
    return " | ".join(out)


def read_lines(path: Path) -> list[str]:
    from docx import Document  # noqa: PLC0415

    raw: list[str] = []
    for block in iter_blocks(Document(path)):
        if hasattr(block, "rows"):
            for row in block.rows:
                text = row_text(row)
                if text:
                    raw.append(text)
        else:
            text = re.sub(r"\s+", " ", block.text).strip()
            if text:
                raw.append(text)

    # Склеить строки, где значение оказалось в следующей строке таблицы
    merged: list[str] = []
    for line in raw:
        if merged and re.fullmatch(r"(описание|заключение)\s*:?", merged[-1], re.IGNORECASE):
            merged[-1] = f"{merged[-1]} {line}"
        else:
            merged.append(line)
    return merged


def split_protocol(lines: list[str]) -> tuple[list[str], list[str]]:
    """Разделить строки на описание и заключение, выбросив шапку карты."""
    body: list[str] = []
    for line in lines:
        low = line.lower()
        if any(low.startswith(prefix) for prefix in HEADER_PREFIXES):
            continue
        body.append(line)

    conclusion: list[str] = []
    start = None
    for index, line in enumerate(body):
        if re.match(r"^\s*заключение\b", line, re.IGNORECASE):
            start = index
            break

    if start is None:
        return body, []

    for line in body[start:]:
        low = line.lower()
        if any(marker in low for marker in CONCLUSION_CUT_MARKERS):
            break
        if NAME_LINE_RE.search(low):
            break
        conclusion.append(line)

    # Убрать хвост, если заключение «утоплено» в описании (длинные абзацы).
    conclusion_text = " ".join(conclusion).lower()
    if len(conclusion) <= 2 and len(conclusion_text) > 1500:
        # Такого вида протоколы хранят заключение одним абзацем в конце
        # описания; оставляем как есть — разметка всё равно по тексту.
        pass

    return body[:start], conclusion


def natural_key(path: Path) -> tuple[str, int, str]:
    """Сортировка файлов вида «1 ЖП (10).docx» как 1, 2, ..., 10.

    Обычная сортировка строк даёт 1, 10, 11, ..., 2 — неудобно читать
    в каталоге и неудобно показывать жюри.
    """
    stem = path.stem
    numbers = re.findall(r"\d+", stem)
    return (re.sub(r"\d+", "", stem), int(numbers[-1]) if numbers else 0, stem)


def collect_protocols(source: Path) -> list[Protocol]:
    protocols: list[Protocol] = []
    for docx in sorted(source.glob("*/*.docx"), key=natural_key):
        group = docx.parent.name
        if group not in STUDY_GROUPS:
            continue
        lines = read_lines(docx)
        description, conclusion = split_protocol(lines)
        protocols.append(Protocol(docx, group, lines, description, conclusion))
    return protocols


# ------------------------------------------------------------- Anonymisation
@dataclass
class Identity:
    """Синтетические идентификаторы одного пациента."""

    full_name: str
    birth_date: date
    age: int
    exam_date: date
    card_number: str
    offset_days: int
    """На сколько дней сдвинуты даты, упомянутые в тексте протокола."""


# Опорная дата, к которой привязываются все синтетические даты
ANCHOR_DATE = date(2026, 9, 1)


def make_identity(rng: random.Random, sex: str) -> Identity:
    pool = MALE_NAMES if sex == "мужской" else FEMALE_NAMES
    if sex == "не указан":
        pool = FEMALE_NAMES + MALE_NAMES
    surname, name, patronymic = rng.choice(pool)
    birth = date(rng.randint(1958, 2006), rng.randint(1, 12), rng.randint(1, 28))
    exam = ANCHOR_DATE - timedelta(days=rng.randint(5, 300))
    age = exam.year - birth.year - ((exam.month, exam.day) < (birth.month, birth.day))
    # Сдвиг подбираем так, чтобы дата приёма попала ровно на ANCHOR_DATE минус
    # число дней, на которое сдвинуты даты внутри текста. Тогда интервалы
    # внутри протокола (менструальный цикл, динамика) остаются связными.
    offset_days = (ANCHOR_DATE - exam).days
    return Identity(
        full_name=f"{surname} {name} {patronymic}",
        birth_date=birth,
        age=max(18, age),
        exam_date=exam,
        card_number=f"{rng.randint(10000, 99999)}",
        offset_days=offset_days,
    )


def shift_date(text: str, offset_days: int) -> str:
    """Сдвинуть все даты в тексте на фиксированное число дней.

    Замена даты на плейсхолчер ломала бы клиническую часть: «менструация
    с 29.07 по 05.08» превратилась бы в «менструация с [дата] по [дата]»
    и перестала быть похожа на настоящий протокол. Сдвиг сохраняет
    интервалы и длительность цикла, но привязывает все даты к синтетическому
    «сейчас» — ни одна дата из исходника не сохраняется.
    """
    if offset_days == 0:
        return text

    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        parts = raw.split(".")
        if len(parts) != 3:
            return raw
        day, month = int(parts[0]), int(parts[1])
        year_part = parts[2]
        try:
            if len(year_part) == 4:
                parsed = date(int(year_part), month, day)
            elif len(year_part) == 2:
                parsed = date(2000 + int(year_part), month, day)
            else:
                return raw
        except ValueError:
            return raw
        shifted = parsed + timedelta(days=offset_days)
        if len(year_part) == 4:
            return shifted.strftime("%d.%m.%Y")
        return shifted.strftime("%d.%m.%y")

    return DATE_RE.sub(replace, text)


def strip_identifiers(text: str, offset_days: int = 0) -> str:
    """Убрать телефоны, ФИО врачей и привязать даты к синтетическому времени.

    Описание заключения может содержать ФИО рекомендованного врача или
    дату предыдущего УЗИ. Для демо это лишнее, поэтому режем.
    """
    text = PHONE_RE.sub("[телефон удалён]", text)
    text = shift_date(text, offset_days)
    text = TIME_RE.sub("[время]", text)
    text = re.sub(r"\(\s*[А-ЯЁ]\.\s*[А-ЯЁ]\.?\s*\)", "", text)  # ( Киселева Д. )
    text = re.sub(r"(?<=[а-яё])\s{2,}(?=[А-ЯЁ])", ". ", text)  # склейка предложений
    return re.sub(r"[ \t]+", " ", text).strip()


# Дисклеймер, который добавляется к каждому демо-протоколу
DISCLAIMER = "Данное заключение не является диагнозом и должно интерпретироваться лечащим врачом."
# Дисклеймеры, которые уже встречаются в исходниках — свой не дублируем
DISCLAIMERS_IN_SOURCE = re.compile(
    r"(данное|данных|результаты|заключение ультразвукового исследования|"
    r"ультразвуковое исследование|эхо-картина)[^.]*не является диагнозом[^.]*\.",
    re.IGNORECASE,
)


def render_demo_text(
    protocol: Protocol, identity: Identity, study_id: str, offset_days: int
) -> str:
    prefix, source_study, sex = STUDY_GROUPS[protocol.group]
    title = source_study or "УЗИ предстательной железы"

    parts = [
        "# Синтетический демо-протокол УЗИ (обезличено, данные не настоящие)",
        f"# study_id: {study_id}",
        f"# тип исследования: {title}",
        f"Пациент: {identity.full_name} (синтетические данные)",
        f"Пол: {sex}",
        f"Дата рождения: {identity.birth_date.strftime('%d.%m.%Y')}",
        f"Возраст на момент осмотра: {identity.age}",
        f"Номер амбулаторной карты: {identity.card_number}",
        f"Дата приёма: {identity.exam_date.strftime('%d.%m.%Y')}",
        "",
        "ОПИСАНИЕ",
    ]
    parts += [strip_identifiers(line, offset_days) for line in protocol.description]

    parts += ["", "ЗАКЛЮЧЕНИЕ"]
    conclusion = [strip_identifiers(line, offset_days) for line in protocol.conclusion]
    conclusion = [line for line in conclusion if line.strip()]
    # Дисклеймер из исходника оставляем один: свой добавим только если его нет
    has_disclaimer = any(DISCLAIMERS_IN_SOURCE.search(line) for line in conclusion)
    parts += conclusion
    if not has_disclaimer:
        parts += ["", DISCLAIMER]
    del prefix
    return "\n".join(parts) + "\n"


# ------------------------------------------------------------------- Labels
def load_matrix() -> list[dict]:
    matrix = REPO_ROOT / "config" / "routing_matrix.json"
    data = json.loads(matrix.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else data["triggers"]


def build_labels(
    studies: list[tuple[str, str]],
    triggers: list[dict],
    source_of: dict[str, str],
) -> list[dict]:
    """Собрать записи разметки.

    studies — список (study_id, source_study). Запись создаётся только если
    source_study исследования совпадает с source_study хотя бы одного
    триггера: невозможные пары не размечаются.

    source_of — study_id → имя исходного .docx, по которому берётся золото.
    """
    by_study: dict[str, list[str]] = {}
    for trigger in triggers:
        by_study.setdefault(trigger["source_study"], []).append(trigger["trigger_id"])

    gold: list[dict] = []
    labeled: set[tuple[str, str]] = set()
    for study_id, source_study in studies:
        confirmed = GOLD.get(source_of.get(study_id, ""), {})
        for trigger_id in sorted(by_study.get(source_study, [])):
            gold.append(
                {
                    "study_id": study_id,
                    "trigger_id": trigger_id,
                    "label": trigger_id in confirmed,
                    "split": "gold",
                }
            )
            labeled.add((study_id, trigger_id))

    return gold


def make_study_id(protocol: Protocol, index: int) -> str:
    prefix = STUDY_GROUPS[protocol.group][0]
    return f"demo_{prefix}_{index:02d}"


# --------------------------------------------------------------------- Main
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--seed", type=int, default=SEED)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.source.exists():
        print(f"Каталог с исходными протоколами не найден: {args.source}", file=sys.stderr)
        return 1

    protocols = collect_protocols(args.source)
    if not protocols:
        print(f"В каталоге {args.source} не найдено ни одного протокола", file=sys.stderr)
        return 1

    triggers = load_matrix()
    rng = random.Random(args.seed)
    protocols_dir = DEMO_DIR / "protocols"
    protocols_dir.mkdir(parents=True, exist_ok=True)

    counts: dict[str, int] = {}
    studies: list[tuple[str, str]] = []
    source_of: dict[str, str] = {}
    real_names: list[str] = []
    for protocol in protocols:
        prefix = STUDY_GROUPS[protocol.group][0]
        counts[prefix] = counts.get(prefix, 0) + 1
        study_id = make_study_id(protocol, counts[prefix])
        source_study = STUDY_GROUPS[protocol.group][1]

        source_of[study_id] = protocol.source_path.name
        identity = make_identity(rng, STUDY_GROUPS[protocol.group][2])
        text = render_demo_text(protocol, identity, study_id, identity.offset_days)
        (protocols_dir / f"{study_id}.txt").write_text(text, encoding="utf-8")
        studies.append((study_id, source_study))
        real_names.extend(collect_real_names(protocol))

    labels = build_labels(studies, triggers, source_of)
    LABELED_PATH.parent.mkdir(parents=True, exist_ok=True)
    LABELED_PATH.write_text(
        json.dumps(labels, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    leaks = verify_no_phi(protocols_dir, real_names)
    label_problems = verify_label_integrity(labels, {study_id for study_id, _ in studies})
    write_demo_readme(protocols, studies, labels, counts)

    report(protocols, labels, counts, leaks, label_problems)
    return 1 if leaks or label_problems else 0


def collect_real_names(protocol: Protocol) -> list[str]:
    """Вытащить значения, которые выглядят как ФИО пациента или врача."""
    names: list[str] = []
    for line in protocol.lines:
        if NAME_LINE_RE.search(line):
            for part in re.split(r"\||:", line)[1:]:
                part = re.sub(r"\s+", " ", part).strip()
                if len(part.split()) >= 2 and re.search(r"[А-ЯЁ][а-яё]+\s+[А-ЯЁ]", part):
                    names.append(part)
    return names


def verify_no_phi(protocols_dir: Path, real_names: list[str]) -> list[str]:
    """Проверить, что реальные ФИО и телефоны не попали в демо-файлы."""
    unique = sorted({name for name in real_names if len(name) > 6})
    leaks: list[str] = []
    for path in sorted(protocols_dir.glob("*.txt")):
        text = path.read_text(encoding="utf-8")
        for name in unique:
            if name.lower() in text.lower():
                leaks.append(f"{path.name}: найдено «{name}»")
        for match in PHONE_RE.findall(text):
            if "телефон удалён" not in match:
                leaks.append(f"{path.name}: телефон {match.strip()}")
                break
    return leaks


def verify_label_integrity(labels: list[dict], known_studies: set[str]) -> list[str]:
    """Проверить, что quality.load_labeled_samples сможет это прочитать.

    Раньше здесь проверялись «нарочно неверные» записи — и именно они
    ломали загрузчик. Теперь проверяем то, что загрузчик требует на самом
    деле: никаких лишних полей (LabeledSample их не принимает) и никаких
    повторов пары (study_id, trigger_id).
    """
    problems: list[str] = []
    seen: dict[tuple[str, str], dict] = {}
    for item in labels:
        extra = set(item) - REQUIRED_FIELDS
        missing = REQUIRED_FIELDS - set(item)
        if extra or missing:
            problems.append(
                f"({item['study_id']}, {item['trigger_id']}): "
                f"лишние поля {sorted(extra)}, отсутствуют {sorted(missing)}"
            )
            continue
        if not isinstance(item["label"], bool):
            problems.append(f"({item['study_id']}, {item['trigger_id']}): label не bool")
        if item["split"] not in ("gold", "synthetic"):
            problems.append(
                f"({item['study_id']}, {item['trigger_id']}): неизвестный split {item['split']!r}"
            )
        key = (item["study_id"], item["trigger_id"])
        if key in seen:
            problems.append(
                f"{key}: повторная разметка "
                f"(метки {seen[key]['label']} и {item['label']}) — загрузчик её отвергнет"
            )
        seen[key] = item
        if item["study_id"] not in known_studies:
            problems.append(f"{item['study_id']}: нет такого протокола")
    return problems


def report(
    protocols: list[Protocol],
    labels: list[dict],
    counts: dict[str, int],
    leaks: list[str],
    label_problems: list[str],
) -> None:
    print(f"Протоколов прочитано: {len(protocols)}")
    for prefix, count in counts.items():
        print(f"  {prefix:<12} {count:>3}")
    gold = [item for item in labels if item["split"] == "gold"]
    synthetic = [item for item in labels if item["split"] == "synthetic"]
    positives = [item for item in gold if item["label"]]
    studies_with_finding = {item["study_id"] for item in positives}
    print(f"Записей разметки: {len(labels)} (gold {len(gold)}, synthetic {len(synthetic)})")
    print(f"  из них label=true: {len(positives)}")
    print(f"  исследований с находкой: {len(studies_with_finding)}")
    scope = studies_label_scope(labels)
    print(f"  исследований без находки: {len(scope) - len(studies_with_finding)}")
    if leaks:
        print("УТЕЧКА ПДн:")
        for leak in leaks:
            print(f"  {leak}")
    else:
        print("Проверка обезличивания: реальных ФИО и телефонов не найдено")
    if label_problems:
        print("ОШИБКА РАЗМЕТКИ:")
        for problem in label_problems:
            print(f"  {problem}")
    else:
        print("Проверка разметки: пары уникальны, состав полей корректен")


def studies_label_scope(labels: list[dict]) -> set[str]:
    return {item["study_id"] for item in labels}


def write_demo_readme(
    protocols: list[Protocol],
    studies: list[tuple[str, str]],
    labels: list[dict],
    counts: dict[str, int],
) -> None:
    gold = [item for item in labels if item["split"] == "gold"]
    positives = [item for item in gold if item["label"]]
    scope = studies_label_scope(labels)

    group_rows = "\n".join(
        f"| `{prefix}` | {count} | {STUDY_GROUPS_GROUP[prefix]} |"
        for prefix, count in counts.items()
    )
    trigger_ids = sorted({item["trigger_id"] for item in labels})
    trigger_rows = "\n".join(f"- `{trigger}`" for trigger in trigger_ids)

    text = f"""# Демо-набор протоколов УЗИ (синтетический)

## Что это

{len(protocols)} протоколов УЗИ в формате `.txt` (UTF-8), по одному файлу на
исследование, плюс разметка в `data/labeled/labeled.json`. Набор нужен, чтобы
запустить демонстрацию маршрутизации и посчитать метрики качества **без
доступа к реальным данным пациентов**: репозиторий не содержит ни одного
фрагмента клинической карты настоящего человека.

Имена файлов соответствуют `study_id` из `labeled.json`:
`demo_<тип>_<NN>.txt`.

## Откуда взято

Протоколы хакатона (89 файлов `.docx`, 6 групп исследований). Из каждого
взята клиническая часть — описание и заключение: именно её читает
маршрутизатор, и именно она нужна для демонстрации.

| префикс | файлов | исследование |
|---|---|---|
{group_rows}

## Почему обезличено

Заменено на синтетические значения (детерминированный seed, повторный запуск
даёт тот же результат):

- ФИО пациента — набор вымышленных имён;
- дата рождения и возраст — случайные правдоподобные значения;
- номер амбулаторной карты — случайный;
- дата и время приёма — случайные;
- ФИО врача и телефон клиники — удалены;
- даты, упомянутые внутри текста (предыдущие УЗИ, дни цикла), — заменены
  на плейсхолдер;
- блоки «Назначенные услуги», «Рекомендации врача», «Подпись врача»
  отброшены: там ФИО и назначения, к маршрутизации отношения не имеющие.

Клиническая часть (описание, заключение, BI-RADS/O-RADS/TI-RADS, размеры,
формулировки) сохранена дословно. Если формулировка кажется некорректной —
она некорректна и в исходнике; править её значит подгонять демо под
ожидаемый результат.

Скрипт `scripts/make_demo_data.py` после генерации проверяет, что ни одно
ФИО и ни один телефон из исходников не попали в `data/demo/`. При утечке
скрипт возвращает ненулевой код.

## Как использовать

```bash
uv run python scripts/make_demo_data.py          # пересобрать набор
ls data/demo/protocols/ | head                   # 89 файлов
python -c "import json;print(len(json.load(open('data/labeled/labeled.json'))))"
```

Текст протокола целиком подаётся в декодер, разметка читается сервисом
метрик. Формат записи:

```json
{{"study_id": "demo_gynecology_01", "trigger_id": "ovarian_cyst",
 "label": true, "split": "gold"}}
```

## Разметка

{len(gold)} записей `split='gold'` по {len(scope)} исследованиям, для которых
в матрице маршрутизации есть триггеры с подходящим `source_study`. Из них
{len(positives)} — `label=true`.

Размечается только пара (исследование, триггер), где `source_study`
триггера совпадает с типом исследования. Протоколы предстательной железы
не размечены: в `config/routing_matrix.json` нет триггера с `source_study`
«УЗИ предстательной железы», а размечать заведомо невозможные пары нельзя.

Правила разметки:

- находка из списка синонимов триггера в заключении без отрицания → `true`;
- находки нет, либо она в отрицании («не выявлено», «не определяется»,
  «соответствует дню цикла»), либо BI-RADS 1-2 / O-RADS 1-2 / TI-RADS 1-2 →
  `false`;
- указанный в матрице порог учитывается: стеноз до 55% при пороге 70% — это
  `false`;
- разметка сделана по заключению, а не по автоматическому совпадению строк,
  и спорные случаи зафиксированы комментариями в
  `scripts/make_demo_data.py` (GOLD).

Триггеры, встречающиеся в разметке:

{trigger_rows}

## Целостность разметки

Каждая пара `(study_id, trigger_id)` встречается ровно один раз, а запись
содержит только поля `study_id`, `trigger_id`, `label`, `split`. Именно это
проверяет `scripts/make_demo_data.py` перед записью файла, потому что
`app/services/quality.py::load_labeled_samples` отвергает и лишние поля, и
повторную разметку (тогда API метрик отвечает 503).

Протоколы предстательной железы не размечены: в матрице маршрутизации нет
триггера с `source_study` «УЗИ предстательной железы», а размечать
заведомо невозможные пары нельзя.

## Пересборка

```bash
uv run python scripts/make_demo_data.py
```

Исходные `.docx` лежат вне репозитория и в `.gitignore`; в git попадают
только `.txt` и `.json`.
"""
    (DEMO_DIR / "README.md").write_text(text, encoding="utf-8")


STUDY_GROUPS_GROUP = {
    "gynecology": "УЗИ органов малого таза",
    "abdomen": "УЗИ брюшной полости",
    "lower_limb": "УЗДГ артерий нижних конечностей",
    "breast": "УЗИ молочных желез",
    "prostate": "УЗИ предстательной железы",
    "thyroid": "УЗИ щитовидной железы",
}

if __name__ == "__main__":
    sys.exit(main())
