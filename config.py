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


def _env_int(name: str, default: int, *, lo: int | None = None,
             hi: int | None = None) -> int:
    """Читает целое из окружения; при пустом/некорректном значении
    возвращает default. Опционально ограничивает диапазон."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if lo is not None and value < lo:
        return default
    if hi is not None and value > hi:
        return default
    return value


_load_dotenv()

#: Токен бота, полученный при создании бота в MAX (обязательно).
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

SUPER_ADMIN_IDS: set[int] = {
    int(x.strip())
    for x in os.getenv("SUPER_ADMIN_ID", "").replace(";", ",").split(",")
    if x.strip().isdigit()
}

#: Необязательный «владелец» бота — добавляется к супер-модераторам.
#: Раньше был захардкожен — вынесен в переменную окружения OWNER_ID.
_owner_id = os.getenv("OWNER_ID", "").strip()
if _owner_id.isdigit():
    SUPER_ADMIN_IDS.add(int(_owner_id))

API_BASE_URL: str = os.getenv("API_BASE_URL", "https://platform-api2.max.ru")

#: Путь к базе данных SQLite.
DB_PATH: Path = Path(os.getenv("DB_PATH", DATA_DIR / "bot.db"))

#: День месяца и час автоматического напоминания неоплатившим.
REMIND_DAY: int = _env_int("REMIND_DAY", 25, lo=1, hi=31)
REMIND_HOUR: int = _env_int("REMIND_HOUR", 12, lo=0, hi=23)

#: Часовой пояс для расчёта времени напоминаний, например Europe/Moscow.
#: Пусто — системный часовой пояс сервера.
TIMEZONE: str = os.getenv("TIMEZONE", "")

#: Таймаут long polling запроса, секунд (по документации MAX: 0..90).
POLL_TIMEOUT: int = _env_int("POLL_TIMEOUT", 30, lo=0, hi=90)


def ensure_dirs() -> None:
    """Создаёт служебные каталоги, если их ещё нет."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
