# -*- coding: utf-8 -*-
"""Точка входа: long polling MAX Bot API + фоновые напоминания.

Запуск:  python bot.py   (настройки в .env, см. .env.example)
"""

from __future__ import annotations

import logging
import sys
import time

import config
import texts
from db import Database
from handlers import BotApp
from maxapi_client import InvalidTokenError, MaxClient, retry
from reminders import ReminderScheduler

log = logging.getLogger("main")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def main() -> int:
    setup_logging()
    config.ensure_dirs()

    if not config.BOT_TOKEN:
        print("ОШИБКА: не задан BOT_TOKEN. Скопируйте .env.example в .env")
        print("и впишите токен бота. Подробнее — в README.md")
        return 1

    client = MaxClient(config.BOT_TOKEN, config.API_BASE_URL, config.POLL_TIMEOUT)
    db = Database(str(config.DB_PATH))
    app = BotApp(client, db)
    app.super_ids = set(config.SUPER_ADMIN_IDS)

    # Проверка токена и получение имени бота
    try:
        me = retry(client.get_me)
        app.bot_name = me.get("name") or "bot"
        log.info("Токен верен, бот: %s (user_id=%s)", app.bot_name, me.get("user_id"))
    except InvalidTokenError:
        print("ОШИБКА: MAX API отклонил токен (401). Проверьте BOT_TOKEN в .env")
        return 1
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось получить /me (%s) — продолжаю работу", exc)

    # Приветствие супер-модераторам при старте
    if app.super_ids:
        for uid in app.super_ids:
            try:
                client.send_text(
                    uid, texts.BOT_STARTED_SUPER.format(bot_name=app.bot_name)
                )
            except Exception:  # noqa: BLE001
                log.warning("Не удалось написать супер-модератору %s", uid)
    else:
        log.warning(
            "SUPER_ADMIN_ID не задан: функции супер-модератора недоступны. "
            "Узнать свой user_id можно командой /id."
        )

    # Фоновые напоминания
    ReminderScheduler(app).start()

    # Основной цикл long polling
    log.info("Бот запущен, жду сообщения (long polling)…")
    while True:
        try:
            data = retry(client.get_updates)
            updates = data.get("updates", []) if isinstance(data, dict) else []
            for upd in updates:
                try:
                    app.handle_update(upd)
                except Exception:  # noqa: BLE001
                    log.exception("Ошибка обработки обновления: %r", upd)
        except InvalidTokenError:
            log.error("Токен стал недействительным (401). Останавливаюсь.")
            return 1
        except KeyboardInterrupt:
            log.info("Остановлено пользователем")
            return 0
        except Exception:  # noqa: BLE001
            log.exception("Сбой цикла polling, пауза 3 секунды")
            time.sleep(3)


if __name__ == "__main__":
    sys.exit(main())
