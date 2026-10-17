#!/usr/bin/env python3
"""Добавляет подписи блоков и лист с расшифровкой колонок. Значения ячеек не меняет."""

from __future__ import annotations

from copy import copy
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

SRC = Path(r"c:\Users\Administrator\Downloads\База Главснаб — итоговый скоринг Checko.xlsx")
DST = Path(r"c:\Users\Administrator\Downloads\База Главснаб — итог с обозначениями.xlsx")

GROUP_FILL_A = PatternFill("solid", fgColor="D9E1F2")
GROUP_FILL_B = PatternFill("solid", fgColor="E2EFDA")
GROUP_FONT = Font(bold=True, size=11)
HEADER_NOTE_FONT = Font(italic=True, size=10, color="404040")

ORIGINAL_HEADERS = [
    ("A", "Организация", "Исходная база", "Название из вашей CRM/базы"),
    ("B", "ИНН", "Исходная база", "Ключ строки; по нему подставлен скоринг"),
    ("C", "Основной вид деятельности по ОКВЭД", "Исходная база", "Как было в исходнике"),
    ("D", "Телефон", "Исходная база", "Контакт из исходника"),
    ("E", "Электронная почта", "Исходная база", "Почта из исходника (не Checko)"),
    ("F", "Основное контактное лицо", "Исходная база", ""),
    ("G", "Тип отношений", "Исходная база", ""),
    ("H", "Адрес", "Исходная база", "Адрес из исходника (не Checko)"),
    ("I", "Комментарий", "Исходная база", ""),
    ("J", "Менеджер (Фамилия)", "Исходная база", ""),
    ("K", "Дата контакта", "Исходная база", ""),
    ("L", "Результат обзвона", "Исходная база", ""),
]

SCORING_HEADERS = [
    ("M", "Светофор", "Скоринг", "Надежный / Рискованный / Нет данных"),
    ("N", "Балл риска", "Скоринг", "Каждый сработавший порог +20"),
    ("O", "Причины", "Скоринг", "Почему рискованный"),
    ("P", "Статус компании", "Checko", "Действующая / ликвидация и т.д."),
    ("Q", "Банкротство", "Checko", "нет / есть сведения"),
    ("R", "Выручка, год 1", "Финансы", "Предпоследний год отчётности"),
    ("S", "Выручка год 1, руб", "Финансы", "Сумма в рублях"),
    ("T", "Выручка, год 2", "Финансы", "Последний год отчётности"),
    ("U", "Выручка год 2, руб", "Финансы", "Сумма в рублях"),
    ("V", "Динамика выручки, %", "Финансы", ""),
    ("W", "Прибыль, год 1", "Финансы", ""),
    ("X", "Прибыль год 1, руб", "Финансы", ""),
    ("Y", "Прибыль, год 2", "Финансы", ""),
    ("Z", "Прибыль год 2, руб", "Финансы", ""),
    ("AA", "Динамика прибыли, %", "Финансы", ""),
    ("AB", "ИП всего", "Исполнительные", "Исполнительные производства"),
    ("AC", "ИП активных", "Исполнительные", ""),
    ("AD", "Задолженность по ИП, руб", "Исполнительные", ""),
    ("AE", "Арбитраж ответчик, дел", "Суды", "Компания ответчик"),
    ("AF", "Арбитраж ответчик, активных", "Суды", ""),
    ("AG", "Сумма требований ответчика, руб", "Суды", ""),
    ("AH", "Оценка Checko, %", "Checko", "Их шкала; в светофор не входит"),
    ("AI", "Название", "Карточка Checko", "С сайта Checko"),
    ("AJ", "ОГРН", "Карточка Checko", ""),
    ("AK", "КПП", "Карточка Checko", ""),
    ("AL", "Дата регистрации", "Карточка Checko", ""),
    ("AM", "ОКВЭД", "Карточка Checko", ""),
    ("AN", "Юридический адрес", "Карточка Checko", "С Checko, не путать с кол. H"),
    ("AO", "Руководитель", "Карточка Checko", ""),
    ("AP", "ИНН руководителя", "Карточка Checko", ""),
    ("AQ", "Телефоны", "Карточка Checko", "С Checko, не путать с кол. D"),
    ("AR", "Электронная почта", "Checko контакты", "С Checko, не путать с кол. E"),
    ("AS", "Сайт", "Checko контакты", ""),
    ("AT", "Ссылка Checko", "Ссылки", "Кликабельная"),
    ("AU", "Ссылка на отчётность", "Ссылки", ""),
    ("AV", "Ссылка на суды", "Ссылки", ""),
    ("AW", "Ссылка на исполнительные", "Ссылки", ""),
    ("AX", "Ссылка на контакты", "Ссылки", ""),
    ("AY", "Ссылка на руководителя", "Ссылки", ""),
    ("AZ", "Ссылка на карту", "Ссылки", "Яндекс.Карты"),
    ("BA", "Ошибка", "Служебное", "Если карточку не удалось разобрать"),
    ("BB", "Проверено", "Служебное", "Дата/время проверки"),
]


def norm_inn(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if value != value:
            return ""
        value = int(value)
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits if len(digits) in (10, 12) else ""


def add_legend_sheet(wb) -> None:
    title = "Обозначения столбцов"
    if title in wb.sheetnames:
        del wb[title]
    ws = wb.create_sheet(title, 0)
    headers = ("Колонка", "Заголовок в файле", "Блок", "Пояснение")
    for col, header in enumerate(headers, 1):
        cell = ws.cell(1, col, header)
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="1F4E79")
        cell.font = Font(bold=True, color="FFFFFF")
    row = 2
    ws.cell(row, 1, "—")
    ws.cell(row, 2, "№ п/п")
    ws.cell(row, 3, "Справочно")
    ws.cell(
        row,
        4,
        "Порядковый номер компании в списке (1, 2, 3…). Не путать с номером строки Excel слева: "
        "строка 200 ≈ 199-я компания, потому что строка 1 — шапка, а часть строк без ИНН пропущена.",
    )
    row += 1
    for letter, name, block, note in ORIGINAL_HEADERS + SCORING_HEADERS:
        ws.cell(row, 1, letter)
        ws.cell(row, 2, name)
        ws.cell(row, 3, block)
        ws.cell(row, 4, note)
        row += 1
    ws.column_dimensions["A"].width = 10
    ws.column_dimensions["B"].width = 36
    ws.column_dimensions["C"].width = 18
    ws.column_dimensions["D"].width = 72
    ws.freeze_panes = "A2"


def enhance_data_sheet(ws) -> None:
    ws.insert_rows(1)
    last_col = ws.max_column
    last_letter = get_column_letter(last_col)

    ws.merge_cells(f"A1:L1")
    a1 = ws["A1"]
    a1.value = "Блок 1 — исходная база (A–L), не изменялась при скоринге"
    a1.fill = GROUP_FILL_A
    a1.font = GROUP_FONT
    a1.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    ws.merge_cells(f"M1:{last_letter}1")
    m1 = ws["M1"]
    m1.value = (
        "Блок 2 — скоринг Checko и карточка компании (M–BB): светофор, цифры, ссылки. "
        "Почта/адрес/телефон в блоке 2 — с Checko; в блоке 1 — из вашей базы."
    )
    m1.fill = GROUP_FILL_B
    m1.font = GROUP_FONT
    m1.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    ws.row_dimensions[1].height = 36

    # Колонка № п/п в конце
    num_col = last_col + 1
    num_letter = get_column_letter(num_col)
    ws.cell(2, num_col, "№ п/п")
    ws.cell(2, num_col).font = Font(bold=True)
    ws.cell(2, num_col).fill = PatternFill("solid", fgColor="FFF2CC")

    counter = 0
    header_row = 2
    for row in range(header_row + 1, ws.max_row + 1):
        if norm_inn(ws.cell(row, 2).value):
            counter += 1
            ws.cell(row, num_col, counter)
        else:
            ws.cell(row, num_col, None)

    note_cell = ws.cell(2, num_col + 1)
    note_cell.value = (
        "Подсказка: слева в Excel номер строки (2, 3… 200) — это не № п/п. "
        f"Смотрите колонку {num_letter}."
    )
    note_cell.font = HEADER_NOTE_FONT
    note_cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells(start_row=2, start_column=num_col + 1, end_row=2, end_column=num_col + 3)

    ws.freeze_panes = "A3"
    if ws.auto_filter.ref:
        end_row = ws.max_row
        ws.auto_filter.ref = f"A2:{num_letter}{end_row}"


def main() -> None:
    wb = load_workbook(SRC)
    add_legend_sheet(wb)
    for name in wb.sheetnames:
        if name in ("Обозначения столбцов", "Правила скоринга"):
            continue
        enhance_data_sheet(wb[name])
    wb.save(DST)
    wb.close()
    print(DST)


if __name__ == "__main__":
    main()
