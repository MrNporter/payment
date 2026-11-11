# -*- coding: utf-8 -*-
"""Выгрузка таблицы оплаты секции в Excel (openpyxl).

Лист «Сводная»: дети × месяцы с отметками оплаты.
Лист «Платежи»: журнал всех отметок (кто, когда, кто отметил).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

if TYPE_CHECKING:
    from db import Database

import config
import utils

HEADER_FILL = PatternFill("solid", fgColor="2F5B7C")
HEADER_FONT = Font(bold=True, color="FFFFFF")
PAID_FILL = PatternFill("solid", fgColor="E2EFDA")
THIN = Side(style="thin", color="B0B0B0")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def _safe_filename(name: str) -> str:
    return re.sub(r"[^\w\s\-.()]", "", name).strip() or "section"


def build_workbook(db: "Database", section) -> Path:
    """Собирает xlsx-файл по секции и возвращает путь к нему."""
    section_id = section["id"]
    children = db.active_children_of_section(section_id)
    current = utils.current_period()
    # сводная начинается с самого раннего события: регистрация первого
    # ребёнка или первая отметка оплаты (платёж может быть за прошлые месяцы)
    candidates = [m for m in (db.earliest_child_month(section_id),) if m]
    payments = db.section_payments(section_id)
    if payments:
        candidates.append(min(r["period"] for r in payments))
    start = min(candidates) if candidates else current
    months = utils.months_range(start, current)

    wb = Workbook()

    # ------------------------------------------------------------------ #
    # Лист 1: сводная таблица
    # ------------------------------------------------------------------ #
    ws = wb.active
    ws.title = "Сводная"
    price_line = (
        f"Стоимость: {int(section['price'])} руб./мес."
        if section["price"] else "Стоимость: не фиксирована"
    )
    ws.append([f"Оплата секции «{section['title']}»", "", price_line,
               f"Сформировано: {utils.now_local().strftime('%d.%m.%Y %H:%M')}"])
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=4 + len(months))
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([])

    header = ["№", "Фамилия", "Имя"] + [utils.period_title(m) for m in months] + ["Оплачено месяцев"]
    ws.append(header)
    header_row = ws.max_row
    for cell in ws[header_row]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER

    paid_total = 0
    for idx, child in enumerate(children, start=1):
        paid_set = db.paid_periods(child["id"])
        row = [idx, child["last_name"], child["first_name"]]
        for m in months:
            row.append("✓" if m in paid_set else "")
            paid_total += 1 if m in paid_set else 0
        row.append(len(paid_set))
        ws.append(row)
        r = ws.max_row
        for col in range(1, 5 + len(months)):
            cell = ws.cell(row=r, column=col)
            cell.border = BORDER
            if 4 <= col <= 3 + len(months) and cell.value == "✓":
                cell.fill = PAID_FILL
                cell.alignment = Alignment(horizontal="center")
            elif col in (1, 3 + len(months) + 1):
                cell.alignment = Alignment(horizontal="center")

    if children:
        ws.append([])
        ws.append([f"Итого детей: {len(children)}; отметок об оплате: {paid_total}"])
        ws.cell(row=ws.max_row, column=1).font = Font(bold=True)

    ws.column_dimensions["A"].width = 5
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 18
    for i in range(len(months)):
        ws.column_dimensions[get_column_letter(4 + i)].width = 13
    ws.column_dimensions[get_column_letter(4 + len(months))].width = 19
    ws.freeze_panes = ws.cell(row=header_row + 1, column=4)

    # ------------------------------------------------------------------ #
    # Лист 2: журнал платежей
    # ------------------------------------------------------------------ #
    wp = wb.create_sheet("Платежи")
    wp.append(["Период", "Фамилия", "Имя", "Сумма, руб.", "Дата отметки", "Отметил"])
    for cell in wp[1]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER
    users_cache: dict[int, str] = {}

    def user_name(uid: int | None) -> str:
        if uid is None:
            return ""
        if uid not in users_cache:
            row = db.get_user(uid)
            if row:
                parts = [x for x in (row["first_name"], row["last_name"]) if x]
                users_cache[uid] = " ".join(parts) or str(uid)
            else:
                users_cache[uid] = str(uid)
        return users_cache[uid]

    for pay in payments:
        wp.append([
            utils.period_title(pay["period"]),
            pay["last_name"],
            pay["first_name"],
            int(pay["amount"]) if pay["amount"] is not None else "",
            (pay["paid_at"] or "")[:10],
            user_name(pay["marked_by"]),
        ])
        for col in range(1, 7):
            wp.cell(row=wp.max_row, column=col).border = BORDER
    for col, width in zip("ABCDEF", (14, 22, 18, 12, 14, 24)):
        wp.column_dimensions[col].width = width
    wp.freeze_panes = "A2"

    config.ensure_dirs()
    out = config.EXPORT_DIR / (
        f"Оплата_{_safe_filename(section['title'])}_{current}.xlsx"
    )
    wb.save(out)
    return out
