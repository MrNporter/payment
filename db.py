# -*- coding: utf-8 -*-
"""Слой базы данных (SQLite). Все запросы бота собраны здесь.

Потокобезопасно: одно соединение + блокировка на время операций
(основной поток — long polling, фоновый поток — напоминания).
"""

from __future__ import annotations

import json
import random
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id    INTEGER PRIMARY KEY,
    first_name TEXT NOT NULL DEFAULT '',
    last_name  TEXT NOT NULL DEFAULT '',
    username   TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sections (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL UNIQUE,
    price      REAL,
    is_active  INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS moderators (
    user_id    INTEGER NOT NULL,
    section_id INTEGER NOT NULL,
    added_at   TEXT NOT NULL,
    PRIMARY KEY (user_id, section_id)
);

CREATE TABLE IF NOT EXISTS children (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id  INTEGER NOT NULL,
    last_name  TEXT NOT NULL,
    first_name TEXT NOT NULL,
    section_id INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending|active|declined
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    child_id  INTEGER NOT NULL,
    period    TEXT NOT NULL,                      -- 'YYYY-MM'
    amount    REAL,
    marked_by INTEGER,
    paid_at   TEXT NOT NULL,
    UNIQUE (child_id, period)
);

CREATE TABLE IF NOT EXISTS states (
    user_id INTEGER PRIMARY KEY,
    state   TEXT NOT NULL,
    ctx     TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_children_parent ON children(parent_id);
CREATE INDEX IF NOT EXISTS idx_children_section ON children(section_id);
CREATE INDEX IF NOT EXISTS idx_payments_child ON payments(child_id);
"""


def utcnow() -> str:
    """Текущее время в ISO-формате (UTC)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Обёртка над SQLite с методами под задачи бота."""

    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ #
    # Пользователи
    # ------------------------------------------------------------------ #
    def upsert_user(
        self, user_id: int, first_name: str, last_name: str, username: str
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO users (user_id, first_name, last_name, username, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    first_name = excluded.first_name,
                    last_name  = excluded.last_name,
                    username   = excluded.username
                """,
                (user_id, first_name, last_name, username, utcnow()),
            )
            self._conn.commit()

    def get_user(self, user_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()

    @staticmethod
    def user_display(row: sqlite3.Row | None) -> str:
        """Человекочитаемое имя пользователя: «Имя Фамилия (@nick)»."""
        if row is None:
            return "неизвестный пользователь"
        parts = [x for x in (row["first_name"], row["last_name"]) if x]
        name = " ".join(parts) if parts else f"пользователь {row['user_id']}"
        if row["username"]:
            name += f" (@{row['username']})"
        return name

    # ------------------------------------------------------------------ #
    # Секции
    # ------------------------------------------------------------------ #
    def add_section(self, title: str, price: float | None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO sections (title, price, is_active, created_at)"
                " VALUES (?, ?, 1, ?)",
                (title, price, utcnow()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def sections(self, active_only: bool = True) -> list[sqlite3.Row]:
        q = "SELECT * FROM sections"
        if active_only:
            q += " WHERE is_active = 1"
        q += " ORDER BY title"
        with self._lock:
            return self._conn.execute(q).fetchall()

    def get_section(self, section_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM sections WHERE id = ?", (section_id,)
            ).fetchone()

    def section_counts(self, section_id: int) -> dict[str, int]:
        """Сколько детей, платежей и модераторов у секции."""
        with self._lock:
            children = self._conn.execute(
                "SELECT COUNT(*) c FROM children WHERE section_id = ?",
                (section_id,),
            ).fetchone()["c"]
            payments = self._conn.execute(
                "SELECT COUNT(*) c FROM payments WHERE child_id IN"
                " (SELECT id FROM children WHERE section_id = ?)",
                (section_id,),
            ).fetchone()["c"]
            moderators = self._conn.execute(
                "SELECT COUNT(*) c FROM moderators WHERE section_id = ?",
                (section_id,),
            ).fetchone()["c"]
        return {"children": children, "payments": payments, "moderators": moderators}

    def delete_section(self, section_id: int) -> dict[str, int]:
        """Полностью удаляет секцию: детей, платежи, модераторов, приглашения."""
        counts = self.section_counts(section_id)
        with self._lock:
            self._conn.execute(
                "DELETE FROM payments WHERE child_id IN"
                " (SELECT id FROM children WHERE section_id = ?)",
                (section_id,),
            )
            self._conn.execute(
                "DELETE FROM children WHERE section_id = ?", (section_id,)
            )
            self._conn.execute(
                "DELETE FROM moderators WHERE section_id = ?", (section_id,)
            )
            self._conn.execute("DELETE FROM sections WHERE id = ?", (section_id,))
            # устаревшие коды приглашения
            for row in self._conn.execute(
                "SELECT key, value FROM settings WHERE key LIKE 'invite:%'"
            ).fetchall():
                try:
                    if int(row["value"].split("|")[0]) == section_id:
                        self._conn.execute(
                            "DELETE FROM settings WHERE key = ?", (row["key"],)
                        )
                except (ValueError, IndexError):
                    pass
            self._conn.commit()
            return counts

    # ------------------------------------------------------------------ #
    # Модераторы
    # ------------------------------------------------------------------ #
    def add_moderator(self, user_id: int, section_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO moderators (user_id, section_id, added_at)"
                " VALUES (?, ?, ?)",
                (user_id, section_id, utcnow()),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def remove_moderator(self, user_id: int, section_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM moderators WHERE user_id = ? AND section_id = ?",
                (user_id, section_id),
            )
            self._conn.commit()

    def section_moderators(self, section_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                """
                SELECT u.* FROM moderators m
                JOIN users u ON u.user_id = m.user_id
                WHERE m.section_id = ?
                ORDER BY u.last_name, u.first_name
                """,
                (section_id,),
            ).fetchall()

    def moderated_sections(self, user_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                """
                SELECT s.* FROM moderators m
                JOIN sections s ON s.id = m.section_id
                WHERE m.user_id = ? AND s.is_active = 1
                ORDER BY s.title
                """,
                (user_id,),
            ).fetchall()

    def is_moderator(self, user_id: int, section_id: int) -> bool:
        with self._lock:
            return (
                self._conn.execute(
                    "SELECT 1 FROM moderators WHERE user_id = ? AND section_id = ?",
                    (user_id, section_id),
                ).fetchone()
                is not None
            )

    # ------------------------------------------------------------------ #
    # Коды приглашения модераторов (действуют 24 часа)
    # ------------------------------------------------------------------ #
    def create_invite(self, section_id: int) -> str:
        code = f"MOD-{random.randint(1000, 9999)}"
        # избегаем коллизий с активными кодами
        while self.get_setting(f"invite:{code}") is not None:
            code = f"MOD-{random.randint(1000, 9999)}"
        from datetime import timedelta

        expires = datetime.now(timezone.utc) + timedelta(hours=24)
        self.set_setting(
            f"invite:{code}", f"{section_id}|{expires.isoformat(timespec='seconds')}"
        )
        return code

    def use_invite(self, code: str) -> int | None:
        """Возвращает section_id, если код валиден; иначе None."""
        raw = self.get_setting(f"invite:{code}")
        if raw is None:
            return None
        self.set_setting(f"invite:{code}", "")  # помечаем использованным
        self.del_setting(f"invite:{code}")
        try:
            section_id_str, expires_str = raw.split("|")
            if datetime.now(timezone.utc) > datetime.fromisoformat(expires_str):
                return None
            return int(section_id_str)
        except (ValueError, IndexError):
            return None

    # ------------------------------------------------------------------ #
    # Дети и заявки
    # ------------------------------------------------------------------ #
    def add_child(
        self, parent_id: int, last_name: str, first_name: str, section_id: int
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO children (parent_id, last_name, first_name,"
                " section_id, status, created_at)"
                " VALUES (?, ?, ?, ?, 'pending', ?)",
                (parent_id, last_name, first_name, section_id, utcnow()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get_child(self, child_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM children WHERE id = ?", (child_id,)
            ).fetchone()

    def children_of(self, parent_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM children WHERE parent_id = ?"
                " ORDER BY created_at",
                (parent_id,),
            ).fetchall()

    def active_children_of_section(self, section_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM children WHERE section_id = ? AND status = 'active'"
                " ORDER BY last_name, first_name",
                (section_id,),
            ).fetchall()

    def pending_children_of_section(self, section_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM children WHERE section_id = ? AND status = 'pending'"
                " ORDER BY created_at",
                (section_id,),
            ).fetchall()

    def has_active_child(self, parent_id: int, section_id: int,
                         last_name: str, first_name: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM children WHERE parent_id = ? AND section_id = ?"
                " AND last_name = ? AND first_name = ? AND status = 'active'",
                (parent_id, section_id, last_name, first_name),
            ).fetchone()
            return row is not None

    def set_child_status(self, child_id: int, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE children SET status = ? WHERE id = ?", (status, child_id)
            )
            self._conn.commit()

    def delete_child(self, child_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM payments WHERE child_id = ?", (child_id,)
            )
            self._conn.execute("DELETE FROM children WHERE id = ?", (child_id,))
            self._conn.commit()

    def delete_declined_child(self, child_id: int) -> None:
        """Удаляет ребёнка со статусом declined (после уведомления родителя)."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM children WHERE id = ? AND status = 'declined'",
                (child_id,),
            )
            self._conn.commit()

    # ------------------------------------------------------------------ #
    # Платежи
    # ------------------------------------------------------------------ #
    def mark_paid(
        self, child_id: int, period: str, amount: float | None, marked_by: int
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO payments (child_id, period, amount, marked_by, paid_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(child_id, period) DO UPDATE SET"
                " amount = excluded.amount, marked_by = excluded.marked_by,"
                " paid_at = excluded.paid_at",
                (child_id, period, amount, marked_by, utcnow()),
            )
            self._conn.commit()

    def unmark_paid(self, child_id: int, period: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM payments WHERE child_id = ? AND period = ?",
                (child_id, period),
            )
            self._conn.commit()

    def is_paid(self, child_id: int, period: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM payments WHERE child_id = ? AND period = ?",
                (child_id, period),
            ).fetchone()

    def paid_periods(self, child_id: int) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT period FROM payments WHERE child_id = ?", (child_id,)
            ).fetchall()
            return {r["period"] for r in rows}

    def section_payments(self, section_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                """
                SELECT p.*, c.last_name, c.first_name, c.parent_id
                FROM payments p JOIN children c ON c.id = p.child_id
                WHERE c.section_id = ?
                ORDER BY p.period DESC, c.last_name
                """,
                (section_id,),
            ).fetchall()

    def earliest_child_month(self, section_id: int) -> str | None:
        """Месяц регистрации самого первого ребёнка секции ('YYYY-MM')."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(created_at) m FROM children WHERE section_id = ?",
                (section_id,),
            ).fetchone()
            if not row or not row["m"]:
                return None
            return row["m"][:7]

    # ------------------------------------------------------------------ #
    # Состояния диалогов
    # ------------------------------------------------------------------ #
    def set_state(self, user_id: int, state: str, ctx: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO states (user_id, state, ctx) VALUES (?, ?, ?)"
                " ON CONFLICT(user_id) DO UPDATE SET state = excluded.state,"
                " ctx = excluded.ctx",
                (user_id, state, json.dumps(ctx or {}, ensure_ascii=False)),
            )
            self._conn.commit()

    def clear_state(self, user_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM states WHERE user_id = ?", (user_id,))
            self._conn.commit()

    def get_state(self, user_id: int) -> tuple[str, dict[str, Any]] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT state, ctx FROM states WHERE user_id = ?", (user_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            ctx = json.loads(row["ctx"])
        except ValueError:
            ctx = {}
        return row["state"], ctx

    # ------------------------------------------------------------------ #
    # Настройки (key-value)
    # ------------------------------------------------------------------ #
    def get_setting(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    def del_setting(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            self._conn.commit()
