# -*- coding: utf-8 -*-
"""Вспомогательные функции: месяцы, периоды, часовые пояса."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import config

MONTHS_RU = [
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
]


def now_local() -> datetime:
    """Текущее время в часовом поясе из config.TIMEZONE (или системном)."""
    if config.TIMEZONE:
        try:
            return datetime.now(ZoneInfo(config.TIMEZONE))
        except Exception:  # noqa: BLE001 — неверная зона, откат на системную
            pass
    return datetime.now()


def current_period() -> str:
    """Текущий месяц в формате 'YYYY-MM'."""
    return now_local().strftime("%Y-%m")


def shift_period(period: str, delta: int) -> str:
    """Сдвигает период 'YYYY-MM' на delta месяцев (может быть отрицательным)."""
    year, month = int(period[:4]), int(period[5:7])
    total = year * 12 + (month - 1) + delta
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def period_title(period: str) -> str:
    """'2026-11' -> 'ноябрь 2026'."""
    year, month = int(period[:4]), int(period[5:7])
    return f"{MONTHS_RU[month - 1]} {year}"


def months_range(start: str, end: str) -> list[str]:
    """Список периодов от start до end включительно."""
    result: list[str] = []
    cur = start
    while cur <= end:
        result.append(cur)
        cur = shift_period(cur, 1)
    return result
