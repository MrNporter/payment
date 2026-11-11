# -*- coding: utf-8 -*-
"""Сборка inline-клавиатур MAX и парсинг payload кнопок.

Кнопка MAX (type=callback):
    {"type": "callback", "text": "Текст", "payload": "p:status",
     "intent": "default" | "positive" | "negative"}

Ограничения платформы: не более 30 рядов и 210 кнопок на сообщение,
текст кнопки — до 64 символов, payload — до 256 символов.
"""

from __future__ import annotations

from typing import Any

POSITIVE = "positive"
NEGATIVE = "negative"


def btn(text: str, payload: str, intent: str | None = None) -> dict[str, Any]:
    """Одна callback-кнопка."""
    button: dict[str, Any] = {"type": "callback", "text": text[:64], "payload": payload[:256]}
    if intent:
        button["intent"] = intent
    return button


def make_kb(*rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Клавиатура из готовых рядов кнопок."""
    return [row for row in rows if row]


def row(*buttons: dict[str, Any]) -> list[dict[str, Any]]:
    """Ряд кнопок."""
    return [b for b in buttons if b]


def back_row(payload: str = "menu", text: str = "⬅️ Главное меню") -> list[dict[str, Any]]:
    """Кнопка «назад»."""
    return [btn(text, payload)]


# ---------------------------------------------------------------------- #
# Payload-конструкторы (единое место формирования строк колбэков)
# ---------------------------------------------------------------------- #
def payload_parts(payload: str) -> list[str]:
    """Разбор payload на части по ':'."""
    return payload.split(":")


# Общие
P_START = "start"
P_MENU = "menu"
P_CANCEL = "cancel"

# Родитель
P_STATUS = "p:status"
P_ADD_CHILD = "p:add"
P_CONTACTS = "p:contacts"
P_WRITE = "p:write"
P_WRITE_CHILD = "p:wr:{child_id}"          # выбор ребёнка для письма
P_REG_SECTION = "p:regsec:{section_id}"    # выбор секции при регистрации

# Секция (модератор/супер)
P_SECTION = "s:{section_id}"
P_TABLE = "t:{section_id}:{period}"
P_TABLE_PREV = "tp:{section_id}:{period}:{page}"
P_CHILD = "c:{section_id}:{period}:{child_id}:{page}"
P_PAY = "pay:{child_id}:{period}:{flag}"   # flag: 1 отметить / 0 снять
P_APPS = "apps:{section_id}"
P_APP_OK = "app:{child_id}:ok"
P_APP_NO = "app:{child_id}:no"
P_WRITE_PARENT = "mw:{section_id}"
P_WRITE_PARENT_CHILD = "mwr:{child_id}"
P_REMIND = "rem:{section_id}"
P_EXCEL = "xls:{section_id}"
P_MODS = "mods:{section_id}"

# Супер-модератор
P_OVERVIEW = "a:over"
P_ADD_SEC = "a:addsec"
P_DEL_SEC = "a:delsec"
P_DEL_SEC_ID = "a:del:{section_id}"
P_DEL_SEC_YES = "a:delyes:{section_id}"
P_MOD_SEC = "a:modsec"
P_MOD_CODE = "a:modcode:{section_id}"
P_UNMOD_SEC = "a:unmod"
P_UNMOD_SEC_ID = "a:unmod1:{section_id}"
P_UNMOD_USER = "a:unmod2:{section_id}:{user_id}"
