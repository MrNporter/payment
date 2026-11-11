# -*- coding: utf-8 -*-
"""Конфигурация бота. Все настройки читаются из переменных окружения
или из файла .env рядом с bot.py (парсится вручную, без внешних библиотек)."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
EXPORT_DIR = DATA_DIR / "exports"


def _load_dotenv() -> None:
    """Простой загрузчик .env (KEY=VALUE) без внешних зависимостей."""
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

#: Токен бота, полученный при создании бота в MAX (обязательно).
BOT_TOKEN: str = os.getenv("f9LHodD0cOL7KcTEWVzFU3dXAUGS8TQVz8JgdrhknWL28FMjOiG5FzS_l9gnGk_vrnDrS43r-LbVP45Ktzo-", "")

#: user_id супер-модератора (можно несколько через запятую).
#: Узнать свой user_id можно командой /id в чате с ботом.
SUPER_ADMIN_IDS: set[int] = { 288965233 }
int(x.strip())
    for x in os.getenv("SUPER_ADMIN_ID", "").replace(";", ",").split(",")
    if x.strip().isdigit()
}

#: Базовый адрес MAX Bot API.
API_BASE_URL: str = os.getenv("API_BASE_URL", "https://platform-api2.max.ru")

#: Путь к базе данных SQLite.
DB_PATH: Path = Path(os.getenv("DB_PATH", DATA_DIR / "bot.db"))

#: День месяца и час автоматического напоминания неоплатившим.
REMIND_DAY: int = int(os.getenv("REMIND_DAY", "25"))
REMIND_HOUR: int = int(os.getenv("REMIND_HOUR", "12"))

#: Часовой пояс для расчёта времени напоминаний, например Europe/Moscow.
#: Пусто — системный часовой пояс сервера.
TIMEZONE: str = os.getenv("TIMEZONE", "")

#: Таймаут long polling запроса, секунд (0..90).
POLL_TIMEOUT: int = int(os.getenv("POLL_TIMEOUT", "30"))


def ensure_dirs() -> None:
    """Создаёт служебные каталоги, если их ещё нет."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
