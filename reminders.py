# -*- coding: utf-8 -*-
"""Фоновые автоматические напоминания неоплатившим родителям.

Отдельный поток раз в минуту проверяет время: в настроенный день
месяца (по умолчанию 25-го) в настроенный час рассылает напоминания
по всем секциям и отправляет сводку супер-модераторам.
Повторный запуск за тот же месяц исключается (метка в settings).
"""

from __future__ import annotations

import logging
import threading

import config

import utils

log = logging.getLogger("reminders")


class ReminderScheduler(threading.Thread):
    """Демон-поток с циклом проверки времени напоминаний."""

    def __init__(self, app):
        super().__init__(daemon=True, name="reminders")
        self.app = app

    def run(self) -> None:
        log.info(
            "Планировщик напоминаний запущен: %d-е число, %d:00",
            config.REMIND_DAY, config.REMIND_HOUR,
        )
        while True:
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                log.exception("Ошибка в планировщике напоминаний")
            threading.Event().wait(60)

    def _tick(self) -> None:
        now = utils.now_local()
        if now.day != config.REMIND_DAY or now.hour != config.REMIND_HOUR:
            return
        period = utils.current_period()
        key = f"autoremind:{period}"
        if self.app.db.get_setting(key) is not None:
            return  # уже разослали в этом месяце
        self.app.db.set_setting(key, "done")
        self.send_auto_reminders(period)

    def send_auto_reminders(self, period: str) -> None:
        """Рассылка по всем активным секциям + сводка супер-модераторам."""
        log.info("Автоматические напоминания за %s", period)
        total = 0
        details: list[str] = []
        for sec in self.app.db.sections():
            count, _names = self.app.send_reminders_for_section(sec["id"], period)
            if count:
                total += count
                details.append(f"«{sec['title']}» — {count}")
        summary = (
            f"🔔 Автоматические напоминания за {utils.period_title(period)} "
            f"отправлены: {total}\n" + "\n".join(details)
            if total else
            f"🔔 Автопоминания за {utils.period_title(period)}: "
            f"все оплатили, рассылка не потребовалась ✅"
        )
        log.info(summary)
        self.app.notify_supers(summary)
