# -*- coding: utf-8 -*-
"""Точка входа: long polling MAX Bot API + фоновые напоминания."""

from __future__ import annotations

import json
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
        return 1

    client = MaxClient(config.BOT_TOKEN, config.API_BASE_URL, config.POLL_TIMEOUT)
    db = Database(str(config.DB_PATH))
    app = BotApp(client, db)
    app.super_ids = set(config.SUPER_ADMIN_IDS)

    try:
        me = retry(client.get_me)
        app.bot_name = me.get("name") or "bot"
        log.info("Токен верен, бот: %s (user_id=%s)", app.bot_name, me.get("user_id"))
    except InvalidTokenError:
        print("ОШИБКА: MAX API отклонил токен (401). Проверьте BOT_TOKEN в .env")
        return 1
    except Exception as exc:
        log.warning("Не удалось получить /me (%s) — продолжаю работу", exc)

    if app.super_ids:
        for uid in app.super_ids:
            try:
                client.send_text(uid, texts.BOT_STARTED_SUPER.format(bot_name=app.bot_name))
            except Exception:
                log.warning("Не удалось написать супер-модератору %s", uid)
    else:
        log.warning("SUPER_ADMIN_ID не задан: функции супер-модератора недоступны.")

    ReminderScheduler(app).start()

    log.info("Бот запущен, жду сообщения (long polling)…")
    while True:
        try:
            data = retry(client.get_updates)
            updates = data.get("updates", []) if isinstance(data, dict) else []
            
            if updates and log.isEnabledFor(logging.DEBUG):
                log.debug("updates: получено %s, marker=%s", len(updates), data.get("marker"))
                for u in updates:
                    log.debug("RAW UPDATE: %s", json.dumps(u, ensure_ascii=False)[:2000])

            for upd in updates:
                try:
                    app.handle_update(upd)
                except Exception:
                    log.exception("Ошибка обработки обновления: %r", upd)
                    
        except InvalidTokenError:
            log.error("Токен стал недействительным (401). Останавливаюсь.")
            return 1
        except KeyboardInterrupt:
            log.info("Остановлено пользователем")
            return 0
        except Exception:
            log.exception("Сбой цикла polling, пауза 3 секунды")
            time.sleep(3)


if __name__ == "__main__":
    sys.exit(main())
