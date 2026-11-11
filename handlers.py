# -*- coding: utf-8 -*-
"""Диалоговая логика бота: роли, меню, заявки, оплата, связь, экспорт.

Класс BotApp не знает, откуда приходят обновления (long polling в
bot.py или фейковый транспорт в тестах) — он только вызывает методы
клиента client.send_text / client.answer_callback / client.send_file.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import keyboards as kb
import texts
import utils
from db import Database
from keyboards import make_kb
from maxapi_client import MaxClient

log = logging.getLogger("bot")

NAME_RE = re.compile(r"^[А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z\s\-]{1,59}$")
INVITE_RE = re.compile(r"^(MOD-\d{4})$", re.IGNORECASE)
CHILDREN_PER_PAGE = 20
APPS_PER_MESSAGE = 10


class BotApp:
    """Ядро бота: маршрутизация обновлений и все сценарии."""

    def __init__(self, client: MaxClient, db: Database, bot_name: str = ""):
        self.client = client
        self.db = db
        self.bot_name = bot_name
        self.super_ids: set[int] = set()

    # ================================================================== #
    # Права доступа
    # ================================================================== #
    def is_super(self, user_id: int) -> bool:
        return user_id in self.super_ids

    def can_section(self, user_id: int, section_id: int) -> bool:
        """Доступ к управлению секцией: супер-модератор или её модератор."""
        return self.is_super(user_id) or self.db.is_moderator(user_id, section_id)

    # ================================================================== #
    # Безопасные обёртки над клиентом
    # ================================================================== #
    def send(self, user_id: int, text: str, buttons=None) -> None:
        try:
            self.client.send_text(user_id, text, buttons)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось отправить сообщение пользователю %s", user_id)

    def safe_answer(
        self,
        callback_id: str,
        text: str | None = None,
        buttons=None,
        notification: str | None = None,
        fallback_user_id: int | None = None,
    ) -> None:
        """Отвечает на нажатие кнопки; при неудаче шлет сообщение напрямую."""
        try:
            self.client.answer_callback(
                callback_id, text=text, buttons=buttons, notification=notification
            )
            return
        except Exception:  # noqa: BLE001
            log.warning("answer_callback не сработал, пробую уведомление", exc_info=True)
        try:
            self.client.answer_callback(callback_id, notification=notification or "Ок")
            if text and fallback_user_id:
                self.send(fallback_user_id, text, buttons)
        except Exception:  # noqa: BLE001
            if text and fallback_user_id:
                self.send(fallback_user_id, text, buttons)
            else:
                log.exception("callback %s остался без ответа", callback_id)

    def notify_section(
        self,
        section_id: int,
        text: str,
        buttons=None,
        exclude: int | None = None,
    ) -> int:
        """Рассылает сообщение модераторам секции; если их нет — суперам."""
        recipients = [
            m["user_id"]
            for m in self.db.section_moderators(section_id)
            if m["user_id"] != exclude
        ]
        if not recipients:
            recipients = list(self.super_ids)
        for uid in recipients:
            self.send(uid, text, buttons)
        return len(recipients)

    def notify_supers(self, text: str, buttons=None) -> None:
        for uid in self.super_ids:
            self.send(uid, text, buttons)

    # ================================================================== #
    # Маршрутизация обновлений
    # ================================================================== #
    def handle_update(self, upd: dict[str, Any]) -> None:
        utype = upd.get("update_type")
        if utype == "message_created":
            self._on_message(upd)
        elif utype == "message_callback":
            self._on_callback(upd)
        elif utype == "bot_started":
            self._on_bot_started(upd)

    def _on_bot_started(self, upd: dict[str, Any]) -> None:
        """Приветствие при первом запуске бота.

        Поле user в bot_started трактуется неоднозначно (пользователь или
        сам бот), поэтому: если user похож на человека — пишем по user_id,
        иначе используем chat_id из обновления.
        """
        chat_id = upd.get("chat_id")
        user = upd.get("user") or {}
        greeting_kb = make_kb(kb.row(kb.btn("🚀 Начать", kb.P_START)))
        if user.get("user_id") and not user.get("is_bot", True):
            self.db.upsert_user(
                user["user_id"],
                user.get("first_name") or "",
                user.get("last_name") or "",
                user.get("username") or "",
            )
            self.send(user["user_id"], texts.GREETING, greeting_kb)
        elif chat_id:
            try:
                self.client.send_text(chat_id, texts.GREETING, greeting_kb, chat=True)
            except Exception:  # noqa: BLE001
                log.exception("Не удалось отправить приветствие в чат %s", chat_id)

    def _on_message(self, upd: dict[str, Any]) -> None:
        message = upd.get("message") or {}
        recipient = message.get("recipient") or {}
        if recipient.get("chat_type") not in (None, "dialog"):
            return  # групповые чаты игнорируем
        sender = message.get("sender") or {}
        user_id = sender.get("user_id")
        if not user_id:
            return
        self.db.upsert_user(
            user_id,
            sender.get("first_name") or "",
            sender.get("last_name") or "",
            sender.get("username") or "",
        )
        body = message.get("body") or {}
        text = (body.get("text") or "").strip()
        if not text:
            self._on_attachment_only(user_id, body)
            return
        self.on_text(user_id, text)

    def _on_attachment_only(self, user_id: int, body: dict[str, Any]) -> None:
        if body.get("attachments"):
            self.send(user_id, texts.ATTACHMENTS_NOT_SUPPORTED)

    def _on_callback(self, upd: dict[str, Any]) -> None:
        callback = upd.get("callback") or {}
        cb_id = callback.get("callback_id")
        user = callback.get("user") or {}
        user_id = user.get("user_id")
        payload = callback.get("payload") or ""
        if not cb_id or not user_id:
            return
        self.db.upsert_user(
            user_id,
            user.get("first_name") or "",
            user.get("last_name") or "",
            user.get("username") or "",
        )
        self.on_callback(cb_id, user_id, payload)

    # ================================================================== #
    # Текстовые сообщения
    # ================================================================== #
    def on_text(self, user_id: int, text: str) -> None:
        # 1. Код приглашения модератора
        m = INVITE_RE.match(text.strip())
        if m:
            self._claim_invite(user_id, m.group(1).upper())
            return

        # 2. Команды
        if text.startswith("/"):
            self._on_command(user_id, text)
            return

        # 3. Диалоговые состояния
        state = self.db.get_state(user_id)
        if state:
            self._on_state_input(user_id, state[0], state[1], text)
            return

        # 4. Свободный текст по роли
        self._on_free_text(user_id, text)

    def _on_command(self, user_id: int, text: str) -> None:
        cmd = text.split()[0].lower().split("@")[0]
        if cmd == "/start":
            self.db.clear_state(user_id)
            children = self.db.children_of(user_id)
            is_staff = self.is_super(user_id) or self.db.moderated_sections(user_id)
            if not children and not is_staff:
                self.db.set_state(user_id, "reg_lname")
                self.send(
                    user_id,
                    texts.WELCOME_NEW + "\n\n" + texts.REG_ASK_LAST_NAME,
                    make_kb(kb.row(kb.btn("⬅️ Отмена", kb.P_CANCEL))),
                )
            else:
                menu_text, menu_kb = self.main_menu(user_id)
                self.send(user_id, menu_text, menu_kb)
        elif cmd == "/menu":
            self.db.clear_state(user_id)
            menu_text, menu_kb = self.main_menu(user_id)
            self.send(user_id, menu_text, menu_kb)
        elif cmd == "/cancel":
            self.db.clear_state(user_id)
            menu_text, menu_kb = self.main_menu(user_id)
            self.send(user_id, texts.CANCELLED + "\n\n" + menu_text, menu_kb)
        elif cmd == "/id":
            self.send(user_id, texts.USER_ID_MSG.format(user_id=user_id))
        elif cmd == "/help":
            self.send(user_id, self._help_text(user_id))
        else:
            self.db.clear_state(user_id)
            menu_text, menu_kb = self.main_menu(user_id)
            self.send(user_id, texts.UNKNOWN_COMMAND + "\n\n" + menu_text, menu_kb)

    def _help_text(self, user_id: int) -> str:
        parts = []
        if self.db.children_of(user_id):
            parts.append(texts.HELP_PARENT)
        if self.db.moderated_sections(user_id):
            parts.append(texts.HELP_MODERATOR)
        if self.is_super(user_id):
            parts.append(texts.HELP_SUPER.format(
                day=utils.now_local().strftime("%d"), hour="12"
            ))
        if not parts:
            parts.append(texts.HELP_PARENT)
        return "\n\n".join(parts)

    def _on_state_input(
        self, user_id: int, state: str, ctx: dict[str, Any], text: str
    ) -> None:
        if state == "reg_lname":
            name = self._normalize_name(text)
            if not name:
                self.send(user_id, texts.REG_BAD_NAME)
                return
            self.db.set_state(user_id, "reg_fname", {"last": name})
            self.send(
                user_id,
                texts.REG_ASK_FIRST_NAME,
                make_kb(kb.row(kb.btn("⬅️ Отмена", kb.P_CANCEL))),
            )
        elif state == "reg_fname":
            name = self._normalize_name(text)
            if not name:
                self.send(user_id, texts.REG_BAD_NAME)
                return
            self.db.set_state(
                user_id, "reg_sec", {"last": ctx.get("last", ""), "first": name}
            )
            self._send_section_picker_for_registration(user_id)
        elif state == "reg_sec":
            # текст вместо кнопки — снова показываем выбор секции
            self._send_section_picker_for_registration(user_id)
        elif state == "p_write":
            child_id = int(ctx.get("child_id", 0))
            self._deliver_parent_message(user_id, child_id, text)
        elif state == "m_write":
            child_id = int(ctx.get("child_id", 0))
            self._deliver_moderator_message(user_id, child_id, text)
        elif state == "a_sec_title":
            title = re.sub(r"\s+", " ", text).strip()
            if not (2 <= len(title) <= 64):
                self.send(
                    user_id,
                    "Название должно быть от 2 до 64 символов. Попробуйте ещё раз.",
                )
                return
            self.db.set_state(user_id, "a_sec_price", {"title": title})
            self.send(
                user_id,
                texts.ADD_SEC_ASK_PRICE,
                make_kb(kb.row(kb.btn("⬅️ Отмена", kb.P_CANCEL))),
            )
        elif state == "a_sec_price":
            self._create_section_from_input(user_id, ctx.get("title", ""), text)
        else:
            self.db.clear_state(user_id)
            self.send(user_id, texts.CANCELLED)

    def _on_free_text(self, user_id: int, text: str) -> None:
        children = [
            c for c in self.db.children_of(user_id) if c["status"] in ("active", "pending")
        ]
        if children:
            self._relay_parent_free_text(user_id, children, text)
            return
        is_staff = self.is_super(user_id) or self.db.moderated_sections(user_id)
        if is_staff:
            self.send(
                user_id,
                "Чтобы ответить родителю — нажмите «Ответить» в его сообщении.\n"
                "Чтобы написать первым — меню секции → «Написать родителю».\n/menu",
            )
            return
        # новый пользователь без детей — предлагаем регистрацию
        menu_text, menu_kb = self.main_menu(user_id)
        self.send(user_id, menu_text, menu_kb)

    # ================================================================== #
    # Главное меню
    # ================================================================== #
    def main_menu(self, user_id: int) -> tuple[str, list[list[dict]]]:
        """Собирает главное меню с учётом ролей пользователя."""
        header_lines: list[str] = [texts.MENU_TITLE]
        rows: list[list[dict]] = []
        children = self.db.children_of(user_id)
        mod_sections = self.db.moderated_sections(user_id)
        is_super = self.is_super(user_id)

        if children or not (mod_sections or is_super):
            header_lines.append("")
            header_lines.append(texts.PARENT_MENU_HEADER)
            rows.append(
                kb.row(
                    kb.btn("💳 Статус оплаты", kb.P_STATUS),
                    kb.btn("➕ Добавить ребёнка", kb.P_ADD_CHILD),
                )
            )
            rows.append(
                kb.row(
                    kb.btn("✉️ Написать модератору", kb.P_WRITE),
                    kb.btn("📞 Контакты", kb.P_CONTACTS),
                )
            )

        if mod_sections:
            header_lines.append("")
            header_lines.append(texts.MOD_MENU_HEADER)
            for sec in mod_sections:
                rows.append(kb.row(kb.btn(f"«{sec['title']}»", kb.P_SECTION.format(section_id=sec["id"]))))

        if is_super:
            header_lines.append("")
            header_lines.append(texts.SUPER_MENU_HEADER)
            rows.append(
                kb.row(
                    kb.btn("🗂 Секции и модераторы", kb.P_OVERVIEW),
                    kb.btn("➕ Добавить секцию", kb.P_ADD_SEC),
                )
            )
            rows.append(
                kb.row(
                    kb.btn("👤 Назначить модератора", kb.P_MOD_SEC),
                    kb.btn("🔓 Снять модератора", kb.P_UNMOD_SEC),
                )
            )
            rows.append(kb.row(kb.btn("➖ Удалить секцию", kb.P_DEL_SEC, kb.NEGATIVE)))

        # Полный доступ ко всем секциям есть только у супер-модератора;
        # модератор видит только свои секции (см. блок выше).
        if is_super:
            all_sections = self.db.sections()
            moderated_ids = {s["id"] for s in mod_sections}
            rest = [s for s in all_sections if s["id"] not in moderated_ids]
            if rest:
                header_lines.append("")
                header_lines.append(texts.ALL_SECTIONS_HEADER)
                for sec in rest:
                    rows.append(
                        kb.row(kb.btn(f"«{sec['title']}»", kb.P_SECTION.format(section_id=sec["id"])))
                    )

        return "\n".join(header_lines), make_kb(*rows)

    # ================================================================== #
    # Нажатия кнопок
    # ================================================================== #
    def on_callback(self, callback_id: str, user_id: int, payload: str) -> None:
        try:
            self._dispatch_callback(callback_id, user_id, payload)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка обработки кнопки %s", payload)
            self.safe_answer(callback_id, notification="⚠️ Произошла ошибка, попробуйте ещё раз")

    def _dispatch_callback(self, callback_id: str, user_id: int, payload: str) -> None:
        parts = kb.payload_parts(payload)
        head = parts[0]

        if payload == kb.P_START or payload == kb.P_MENU:
            self.db.clear_state(user_id)
            text, buttons = self.main_menu(user_id)
            self.safe_answer(callback_id, text=text, buttons=buttons, fallback_user_id=user_id)

        elif payload == kb.P_CANCEL:
            self.db.clear_state(user_id)
            text, buttons = self.main_menu(user_id)
            self.safe_answer(
                callback_id,
                text=texts.CANCELLED + "\n\n" + text,
                buttons=buttons,
                fallback_user_id=user_id,
            )

        elif head == "p":
            if len(parts) >= 3 and parts[1] == "regsec":
                self._registration_pick_section(callback_id, user_id, int(parts[2]))
            else:
                self._parent_callback(callback_id, user_id, payload, parts)

        elif head in ("s", "t", "tp", "c", "pay", "apps", "app", "mw", "mwr", "rem", "xls", "mods"):
            self._moderator_callback(callback_id, user_id, payload, parts)

        elif head == "a":
            self._super_callback(callback_id, user_id, payload, parts)

        else:
            self.safe_answer(callback_id, notification="Кнопка неактуальна — /menu")

    # ------------------------------------------------------------------ #
    # Родительские кнопки
    # ------------------------------------------------------------------ #
    def _parent_callback(
        self, callback_id: str, user_id: int, payload: str, parts: list[str]
    ) -> None:
        if payload == kb.P_STATUS:
            text = self._status_view(user_id)
            self.safe_answer(callback_id, text=text, buttons=make_kb(kb.back_row()), fallback_user_id=user_id)

        elif payload == kb.P_ADD_CHILD:
            self.db.set_state(user_id, "reg_lname")
            self.safe_answer(
                callback_id,
                text=texts.REG_ASK_LAST_NAME,
                buttons=make_kb(kb.row(kb.btn("⬅️ Отмена", kb.P_CANCEL))),
                fallback_user_id=user_id,
            )

        elif payload == kb.P_CONTACTS:
            text = self._contacts_view(user_id)
            self.safe_answer(callback_id, text=text, buttons=make_kb(kb.back_row()), fallback_user_id=user_id)

        elif payload == kb.P_WRITE:
            self._start_parent_write(callback_id, user_id)

        elif len(parts) == 3 and parts[1] == "wr":
            child_id = int(parts[2])
            self._set_parent_write_state(callback_id, user_id, child_id)

        else:
            self.safe_answer(callback_id, notification="Кнопка неактуальна — /menu")

    def _status_view(self, user_id: int) -> str:
        children = self.db.children_of(user_id)
        if not children:
            return texts.PARENT_STATUS_NO_CHILDREN
        lines = [texts.PARENT_STATUS_TITLE, ""]
        for child in children:
            sec = self.db.get_section(child["section_id"])
            sec_title = sec["title"] if sec else "?"
            head = texts.PARENT_STATUS_CHILD.format(
                last=child["last_name"], first=child["first_name"], section=sec_title
            )
            if child["status"] == "pending":
                lines.append(f"{head} — {texts.PARENT_STATUS_PENDING}")
                continue
            if child["status"] == "declined":
                lines.append(f"{head} — {texts.PARENT_STATUS_DECLINED}")
                continue
            price_line = ""
            if sec and sec["price"]:
                price_line = " " + texts.PARENT_STATUS_PRICE.format(
                    price=int(sec["price"])
                )
            lines.append(head + price_line)
            paid = self.db.paid_periods(child["id"])
            start = (child["created_at"] or "")[:7]
            for period in utils.months_range(start, utils.current_period()):
                mark = "✅" if period in paid else "❌"
                lines.append(f"   {utils.period_title(period)} {mark}")
            lines.append("")
        return "\n".join(lines).rstrip()

    def _contacts_view(self, user_id: int) -> str:
        children = self.db.children_of(user_id)
        sections: dict[int, str] = {}
        for child in children:
            sec = self.db.get_section(child["section_id"])
            if sec and sec["is_active"]:
                sections[sec["id"]] = sec["title"]
        if not sections:
            return texts.PARENT_STATUS_NO_CHILDREN
        lines = [texts.CONTACTS_TITLE, ""]
        for sec_id, title in sections.items():
            lines.append(texts.CONTACTS_SECTION.format(section=title))
            mods = self.db.section_moderators(sec_id)
            if mods:
                for mod in mods:
                    lines.append("• " + self.db.user_display(mod))
            else:
                lines.append("• " + texts.CONTACTS_EMPTY.format(section=title))
            lines.append("")
        return "\n".join(lines).rstrip()

    def _start_parent_write(self, callback_id: str, user_id: int) -> None:
        children = [
            c for c in self.db.children_of(user_id)
            if c["status"] in ("active", "pending")
        ]
        if not children:
            self.safe_answer(callback_id, text=texts.PARENT_STATUS_NO_CHILDREN,
                             buttons=make_kb(kb.back_row()), fallback_user_id=user_id)
            return
        if len(children) == 1:
            self._set_parent_write_state(callback_id, user_id, children[0]["id"])
            return
        rows = []
        for child in children:
            sec = self.db.get_section(child["section_id"])
            title = sec["title"] if sec else ""
            rows.append(
                kb.row(kb.btn(
                    f"{child['last_name']} {child['first_name']} ({title})",
                    kb.P_WRITE_CHILD.format(child_id=child["id"]),
                ))
            )
        rows.append(kb.back_row())
        self.safe_answer(callback_id, text=texts.PARENT_WRITE_PICK, buttons=make_kb(*rows),
                         fallback_user_id=user_id)

    def _set_parent_write_state(self, callback_id: str, user_id: int, child_id: int) -> None:
        child = self.db.get_child(child_id)
        if not child or child["parent_id"] != user_id:
            self.safe_answer(callback_id, notification="Ребёнок не найден — /menu")
            return
        sec = self.db.get_section(child["section_id"])
        self.db.set_state(user_id, "p_write", {"child_id": child_id})
        self.safe_answer(
            callback_id,
            text=texts.PARENT_WRITE_ASK.format(
                section=sec["title"] if sec else "?",
                child=f"{child['last_name']} {child['first_name']}",
            ),
            buttons=make_kb(kb.row(kb.btn("⬅️ Отмена", kb.P_CANCEL))),
            fallback_user_id=user_id,
        )

    # ------------------------------------------------------------------ #
    # Модераторские кнопки (и супер имеет к ним доступ)
    # ------------------------------------------------------------------ #
    def _moderator_callback(
        self, callback_id: str, user_id: int, payload: str, parts: list[str]
    ) -> None:
        # pay и app работают от id ребёнка — права проверяем по его секции
        if parts[0] == "pay":
            child = self.db.get_child(int(parts[1]))
            period, flag = parts[2], parts[3]
            if not child or not self.can_section(user_id, child["section_id"]):
                self.safe_answer(callback_id, notification="Недостаточно прав")
                return
            self._toggle_payment(callback_id, user_id, child, period, flag)
            return

        if parts[0] == "app":
            child = self.db.get_child(int(parts[1]))
            decision = parts[2]
            if not child or not self.can_section(user_id, child["section_id"]):
                self.safe_answer(callback_id, notification="Недостаточно прав")
                return
            if child["status"] != "pending":
                self.safe_answer(callback_id, notification="Заявка уже обработана")
                return
            self._decide_application(callback_id, user_id, child, decision == "ok")
            return

        if parts[0] == "mwr":
            child = self.db.get_child(int(parts[1]))
            if not child or not self.can_section(user_id, child["section_id"]):
                self.safe_answer(callback_id, notification="Недостаточно прав")
                return
            sec = self.db.get_section(child["section_id"])
            self.db.set_state(user_id, "m_write", {"child_id": child["id"]})
            self.safe_answer(
                callback_id,
                text=texts.WRITE_PARENT_ASK.format(
                    last=child["last_name"], first=child["first_name"],
                    section=sec["title"] if sec else "?",
                ),
                buttons=make_kb(kb.row(kb.btn("⬅️ Отмена", kb.P_CANCEL))),
                fallback_user_id=user_id,
            )
            return

        # всё остальное начинается с id секции
        section_id = int(parts[1])
        if not self.can_section(user_id, section_id):
            self.safe_answer(callback_id, notification="Недостаточно прав")
            return
        sec = self.db.get_section(section_id)
        if not sec:
            self.safe_answer(callback_id, notification="Секция не найдена — /menu")
            return

        if parts[0] == "s":
            text, buttons = self._section_menu(sec)
            self.safe_answer(callback_id, text=text, buttons=buttons, fallback_user_id=user_id)

        elif parts[0] == "t":
            period = parts[2]
            text, buttons = self._table_view(sec, period)
            self.safe_answer(callback_id, text=text, buttons=buttons, fallback_user_id=user_id)

        elif parts[0] == "tp":
            period, page = parts[2], int(parts[3])
            text, buttons = self._table_pick(sec, period, page)
            self.safe_answer(callback_id, text=text, buttons=buttons, fallback_user_id=user_id)

        elif parts[0] == "c":
            period, child_id, page = parts[2], int(parts[3]), int(parts[4])
            child = self.db.get_child(child_id)
            if not child or child["section_id"] != section_id:
                self.safe_answer(callback_id, notification="Ребёнок не найден")
                return
            text, buttons = self._child_card(sec, period, child, page)
            self.safe_answer(callback_id, text=text, buttons=buttons, fallback_user_id=user_id)

        elif parts[0] == "apps":
            text, buttons = self._apps_view(sec)
            self.safe_answer(callback_id, text=text, buttons=buttons, fallback_user_id=user_id)

        elif parts[0] == "mw":
            text, buttons = self._write_parent_pick(sec)
            self.safe_answer(callback_id, text=text, buttons=buttons, fallback_user_id=user_id)

        elif parts[0] == "rem":
            text, buttons = self._remind_section(user_id, sec)
            self.safe_answer(callback_id, text=text, buttons=buttons, fallback_user_id=user_id)

        elif parts[0] == "xls":
            self.safe_answer(callback_id, notification=texts.EXCEL_PREPARE)
            self._send_excel(user_id, sec)

        elif parts[0] == "mods":
            text, buttons = self._mods_view(sec)
            self.safe_answer(callback_id, text=text, buttons=buttons, fallback_user_id=user_id)

        else:
            self.safe_answer(callback_id, notification="Кнопка неактуальна — /menu")

    def _section_menu(self, sec) -> tuple[str, list[list[dict]]]:
        pending = len(self.db.pending_children_of_section(sec["id"]))
        apps_label = f"📥 Заявки ({pending})" if pending else "📥 Заявки"
        rows = make_kb(
            kb.row(
                kb.btn("📋 Таблица оплаты", kb.P_TABLE.format(
                    section_id=sec["id"], period=utils.current_period())),
                kb.btn(apps_label, kb.P_APPS.format(section_id=sec["id"])),
            ),
            kb.row(
                kb.btn("✉️ Написать родителю", kb.P_WRITE_PARENT.format(section_id=sec["id"])),
                kb.btn("🔔 Напомнить", kb.P_REMIND.format(section_id=sec["id"])),
            ),
            kb.row(
                kb.btn("📤 Excel", kb.P_EXCEL.format(section_id=sec["id"])),
                kb.btn("👥 Модераторы", kb.P_MODS.format(section_id=sec["id"])),
            ),
            kb.back_row(),
        )
        return texts.SECTION_MENU_TITLE.format(section=sec["title"]), rows

    def _table_view(self, sec, period: str) -> tuple[str, list[list[dict]]]:
        children = self.db.active_children_of_section(sec["id"])
        rows: list[list[dict]] = []
        if not children:
            text = texts.TABLE_EMPTY.format(section=sec["title"], period=utils.period_title(period))
        else:
            lines = []
            paid_count = 0
            for child in children:
                is_paid = self.db.is_paid(child["id"], period) is not None
                paid_count += 1 if is_paid else 0
                lines.append(f"{'✅' if is_paid else '❌'} {child['last_name']} {child['first_name']}")
            text = (
                texts.TABLE_TITLE.format(
                    section=sec["title"], period=utils.period_title(period),
                    paid=paid_count, total=len(children),
                )
                + "\n\n" + "\n".join(lines) + texts.TABLE_FOOTER
            )
        rows.append(kb.row(kb.btn(
            "✍️ Отметить оплату",
            kb.P_TABLE_PREV.format(section_id=sec["id"], period=period, page=0),
        )))
        cur = utils.current_period()
        prev = utils.shift_period(period, -1)
        if period < cur:
            nxt = utils.shift_period(period, 1)
            rows.append(kb.row(
                kb.btn("◀️ " + utils.period_title(prev),
                       kb.P_TABLE.format(section_id=sec["id"], period=prev)),
                kb.btn(utils.period_title(nxt) + " ▶️",
                       kb.P_TABLE.format(section_id=sec["id"], period=nxt)),
            ))
        else:
            rows.append(kb.row(kb.btn(
                "◀️ " + utils.period_title(prev),
                kb.P_TABLE.format(section_id=sec["id"], period=prev))))
        rows.append(kb.row(kb.btn("⬅️ Меню секции", kb.P_SECTION.format(section_id=sec["id"]))))
        return text, make_kb(*rows)

    def _table_pick(self, sec, period: str, page: int) -> tuple[str, list[list[dict]]]:
        children = self.db.active_children_of_section(sec["id"])
        total_pages = max(1, (len(children) + CHILDREN_PER_PAGE - 1) // CHILDREN_PER_PAGE)
        page = max(0, min(page, total_pages - 1))
        chunk = children[page * CHILDREN_PER_PAGE:(page + 1) * CHILDREN_PER_PAGE]
        rows = []
        for child in chunk:
            is_paid = self.db.is_paid(child["id"], period) is not None
            rows.append(kb.row(kb.btn(
                ("✅ " if is_paid else "❌ ") + f"{child['last_name']} {child['first_name']}",
                kb.P_CHILD.format(
                    section_id=sec["id"], period=period, child_id=child["id"], page=page),
            )))
        if total_pages > 1:
            nav = []
            if page > 0:
                nav.append(kb.btn("◀️", kb.P_TABLE_PREV.format(
                    section_id=sec["id"], period=period, page=page - 1)))
            nav.append(kb.btn(f"{page + 1}/{total_pages}", "noop"))
            if page < total_pages - 1:
                nav.append(kb.btn("▶️", kb.P_TABLE_PREV.format(
                    section_id=sec["id"], period=period, page=page + 1)))
            rows.append(kb.row(*nav))
        rows.append(kb.row(kb.btn(
            "⬅️ К таблице",
            kb.P_TABLE.format(section_id=sec["id"], period=period))))
        return texts.TABLE_PICK_TITLE.format(
            section=sec["title"], period=utils.period_title(period)), make_kb(*rows)

    def _child_card(self, sec, period: str, child, page: int) -> tuple[str, list[list[dict]]]:
        paid = self.db.is_paid(child["id"], period)
        price_line = ""
        if sec["price"]:
            price_line = f" ({int(sec['price'])} ₽/мес)"
        if paid:
            date = (paid["paid_at"] or "")[:10]
            period_line = texts.CHILD_PAID_LINE.format(
                period=utils.period_title(period), date=date)
            toggle = kb.btn(
                f"↩️ Снять оплату — {utils.period_title(period)}",
                kb.P_PAY.format(child_id=child["id"], period=period, flag=0),
                kb.NEGATIVE,
            )
        else:
            period_line = texts.CHILD_UNPAID_LINE.format(period=utils.period_title(period))
            toggle = kb.btn(
                f"✅ Отметить оплату — {utils.period_title(period)}",
                kb.P_PAY.format(child_id=child["id"], period=period, flag=1),
                kb.POSITIVE,
            )

        start = (child["created_at"] or "")[:7]
        months = [p for p in utils.months_range(start, period)][-6:]
        paid_set = self.db.paid_periods(child["id"])
        if months:
            history = "\n".join(
                (texts.CHILD_PAID_LINE.format(period=utils.period_title(p), date="").rstrip()
                 if p in paid_set else
                 texts.CHILD_UNPAID_LINE.format(period=utils.period_title(p)))
                for p in months
            )
        else:
            history = texts.CHILD_NO_HISTORY

        text = texts.CHILD_CARD.format(
            last=child["last_name"], first=child["first_name"],
            section=sec["title"], price_line=price_line,
            period_line=period_line, history_label="Последние месяцы:", history=history,
        )
        rows = make_kb(
            kb.row(toggle),
            kb.row(
                kb.btn("◀️", kb.P_CHILD.format(
                    section_id=sec["id"], period=utils.shift_period(period, -1),
                    child_id=child["id"], page=page)),
                kb.btn("⬅️ К списку", kb.P_TABLE_PREV.format(
                    section_id=sec["id"], period=period, page=page)),
            ),
            kb.row(kb.btn("⬅️ Меню секции", kb.P_SECTION.format(section_id=sec["id"]))),
        )
        return text, rows

    def _toggle_payment(self, callback_id, user_id, child, period: str, flag: str) -> None:
        sec = self.db.get_section(child["section_id"])
        if flag == "1":
            amount = sec["price"] if sec else None
            self.db.mark_paid(child["id"], period, amount, user_id)
            notification = "✅ Оплата отмечена"
        else:
            self.db.unmark_paid(child["id"], period)
            notification = "↩️ Отметка снята"
        text, buttons = self._child_card(sec, period, child, 0)
        self.safe_answer(callback_id, text=text, buttons=buttons,
                         notification=notification, fallback_user_id=user_id)

    def _apps_view(self, sec) -> tuple[str, list[list[dict]]]:
        pending = self.db.pending_children_of_section(sec["id"])
        if not pending:
            return texts.APPS_EMPTY.format(section=sec["title"]), make_kb(kb.back_row(
                kb.P_SECTION.format(section_id=sec["id"]), "⬅️ Меню секции"))
        lines = [texts.APPS_TITLE.format(section=sec["title"]), ""]
        rows = []
        for child in pending[:APPS_PER_MESSAGE]:
            parent = self.db.get_user(child["parent_id"])
            lines.append(texts.APP_ITEM.format(
                last=child["last_name"], first=child["first_name"],
                parent=self.db.user_display(parent)))
            rows.append(kb.row(
                kb.btn(f"✅ {child['last_name']} {child['first_name']}",
                       kb.P_APP_OK.format(child_id=child["id"]), kb.POSITIVE),
                kb.btn("❌",
                       kb.P_APP_NO.format(child_id=child["id"]), kb.NEGATIVE),
            ))
        if len(pending) > APPS_PER_MESSAGE:
            lines.append("")
            lines.append(texts.APPS_MORE.format(count=len(pending) - APPS_PER_MESSAGE))
        rows.append(kb.back_row(kb.P_SECTION.format(section_id=sec["id"]), "⬅️ Меню секции"))
        return "\n".join(lines), make_kb(*rows)

    def _decide_application(self, callback_id, user_id, child, accept: bool) -> None:
        sec = self.db.get_section(child["section_id"])
        moderator = self.db.get_user(user_id)
        if accept:
            self.db.set_child_status(child["id"], "active")
            self.send(
                child["parent_id"],
                texts.APP_ACCEPTED_PARENT.format(
                    last=child["last_name"], first=child["first_name"],
                    section=sec["title"], moderator=self.db.user_display(moderator)),
            )
            result = texts.APP_DONE_MODERATOR.format(
                last=child["last_name"], first=child["first_name"])
        else:
            self.db.set_child_status(child["id"], "declined")
            self.send(
                child["parent_id"],
                texts.APP_DECLINED_PARENT.format(
                    last=child["last_name"], first=child["first_name"],
                    section=sec["title"]),
            )
            result = texts.APP_DECLINED_MODERATOR.format(
                last=child["last_name"], first=child["first_name"])
        text, buttons = self._apps_view(sec)
        self.safe_answer(callback_id, text=result + "\n\n" + text, buttons=buttons,
                         notification=result.split("\n")[0], fallback_user_id=user_id)

    def _write_parent_pick(self, sec) -> tuple[str, list[list[dict]]]:
        children = self.db.active_children_of_section(sec["id"])
        if not children:
            return texts.TABLE_EMPTY.format(
                section=sec["title"], period=utils.current_period()
            ), make_kb(kb.back_row(kb.P_SECTION.format(section_id=sec["id"]), "⬅️ Меню секции"))
        rows = []
        for child in children[:CHILDREN_PER_PAGE]:
            rows.append(kb.row(kb.btn(
                f"{child['last_name']} {child['first_name']}",
                kb.P_WRITE_PARENT_CHILD.format(child_id=child["id"]))))
        rows.append(kb.back_row(kb.P_SECTION.format(section_id=sec["id"]), "⬅️ Меню секции"))
        return texts.WRITE_PARENT_PICK.format(section=sec["title"]), make_kb(*rows)

    def _mods_view(self, sec) -> tuple[str, list[list[dict]]]:
        mods = self.db.section_moderators(sec["id"])
        if not mods:
            text = texts.MODS_LIST_EMPTY.format(section=sec["title"])
        else:
            lines = [texts.MODS_LIST_TITLE.format(section=sec["title"]), ""]
            lines += [f"• {self.db.user_display(m)}" for m in mods]
            text = "\n".join(lines)
        return text, make_kb(kb.back_row(kb.P_SECTION.format(section_id=sec["id"]), "⬅️ Меню секции"))

    def _remind_section(self, user_id: int, sec) -> tuple[str, list[list[dict]]]:
        count, names = self.send_reminders_for_section(sec["id"], utils.current_period())
        if count == 0:
            text = texts.REMIND_NONEED.format(
                period=utils.period_title(utils.current_period()), section=sec["title"])
        else:
            text = texts.REMIND_RESULT.format(
                period=utils.period_title(utils.current_period()),
                count=count, list="; ".join(names))
        return text, make_kb(kb.back_row(kb.P_SECTION.format(section_id=sec["id"]), "⬅️ Меню секции"))

    def send_reminders_for_section(self, section_id: int, period: str) -> tuple[int, list[str]]:
        """Рассылает напоминания родителям неоплативших детей. Возвращает счёт."""
        sec = self.db.get_section(section_id)
        if not sec:
            return 0, []
        price = f", {int(sec['price'])} ₽/мес" if sec["price"] else ""
        names: list[str] = []
        for child in self.db.active_children_of_section(section_id):
            if self.db.is_paid(child["id"], period) is not None:
                continue
            self.send(
                child["parent_id"],
                texts.REMIND_TO_PARENT.format(
                    period=utils.period_title(period),
                    last=child["last_name"], first=child["first_name"],
                    section=sec["title"], price=price),
            )
            names.append(f"{child['last_name']} {child['first_name']}")
        return len(names), names

    # ================================================================== #
    # Регистрация ребёнка (родитель)
    # ================================================================== #
    def _send_section_picker_for_registration(self, user_id: int) -> None:
        sections = self.db.sections()
        if not sections:
            self.db.clear_state(user_id)
            self.send(user_id, texts.REG_NO_SECTIONS, make_kb(kb.back_row()))
            return
        rows = [
            kb.row(kb.btn(f"«{s['title']}»", kb.P_REG_SECTION.format(section_id=s["id"])))
            for s in sections
        ]
        rows.append(kb.row(kb.btn("⬅️ Отмена", kb.P_CANCEL)))
        self.send(user_id, texts.REG_ASK_SECTION, make_kb(*rows))

    def _registration_pick_section(self, callback_id: str, user_id: int, section_id: int) -> None:
        state = self.db.get_state(user_id)
        if not state or state[0] != "reg_sec":
            self.safe_answer(callback_id, notification="Сначала заполните шаги 1 и 2")
            return
        ctx = state[1]
        sec = self.db.get_section(section_id)
        if not sec or not sec["is_active"]:
            self.safe_answer(callback_id, notification="Секция не найдена")
            return
        if self.db.has_active_child(user_id, section_id, ctx["last"], ctx["first"]) or \
                any(
                    c for c in self.db.children_of(user_id)
                    if c["status"] == "pending" and c["section_id"] == section_id
                    and c["last_name"] == ctx["last"] and c["first_name"] == ctx["first"]
                ):
            self.db.clear_state(user_id)
            self.safe_answer(
                callback_id,
                text=texts.REG_DUPLICATE.format(
                    last=ctx["last"], first=ctx["first"], section=sec["title"]),
                buttons=make_kb(kb.back_row()),
                fallback_user_id=user_id,
            )
            return
        child_id = self.db.add_child(user_id, ctx["last"], ctx["first"], section_id)
        self.db.clear_state(user_id)
        parent = self.db.get_user(user_id)
        self.notify_section(
            section_id,
            texts.NEW_APP_TO_MODS.format(
                section=sec["title"], last=ctx["last"], first=ctx["first"],
                parent=self.db.user_display(parent)),
            buttons=make_kb(kb.row(
                kb.btn(f"✅ {ctx['last']} {ctx['first']}",
                       kb.P_APP_OK.format(child_id=child_id), kb.POSITIVE),
                kb.btn("❌", kb.P_APP_NO.format(child_id=child_id), kb.NEGATIVE),
            )),
            exclude=user_id,
        )
        self.safe_answer(
            callback_id,
            text=texts.REG_DONE.format(
                last=ctx["last"], first=ctx["first"], section=sec["title"]),
            buttons=make_kb(kb.back_row()),
            notification="Заявка отправлена",
            fallback_user_id=user_id,
        )

    # ================================================================== #
    # Пересылка сообщений родитель <-> модераторы
    # ================================================================== #
    def _deliver_parent_message(self, user_id: int, child_id: int, text: str) -> None:
        child = self.db.get_child(child_id)
        if not child or child["parent_id"] != user_id:
            self.db.clear_state(user_id)
            self.send(user_id, texts.CANCELLED)
            return
        self._relay_child_message(user_id, child, text, confirm=True)
        self.db.clear_state(user_id)

    def _relay_parent_free_text(self, user_id: int, children, text: str) -> None:
        by_section: dict[int, list] = {}
        for child in children:
            by_section.setdefault(child["section_id"], []).append(child)
        delivered: list[str] = []
        for section_id, kids in by_section.items():
            sec = self.db.get_section(section_id)
            if not sec or not sec["is_active"]:
                continue
            self._relay_to_section(user_id, sec, kids, text)
            delivered.append(sec["title"])
        if delivered:
            self.send(user_id, "✅ Сообщение передано модераторам секции: " + ", ".join(delivered) + ".")

    def _relay_child_message(self, user_id: int, child, text: str, confirm: bool) -> None:
        sec = self.db.get_section(child["section_id"])
        if not sec:
            return
        kids = [
            c for c in self.db.children_of(user_id)
            if c["section_id"] == child["section_id"] and c["status"] in ("active", "pending")
        ]
        self._relay_to_section(user_id, sec, kids, text)
        if confirm:
            self.send(user_id, texts.PARENT_WRITE_CONFIRM.format(section=sec["title"]))

    def _relay_to_section(self, user_id: int, sec, kids, text: str) -> None:
        parent = self.db.get_user(user_id)
        names = ", ".join(f"{c['last_name']} {c['first_name']}" for c in kids)
        buttons = make_kb(*[
            kb.row(kb.btn(
                texts.RELAY_REPLY_BTN + f" — {c['last_name']} {c['first_name'][:1]}.",
                kb.P_WRITE_PARENT_CHILD.format(child_id=c["id"])))
            for c in kids
        ])
        mods = self.db.section_moderators(sec["id"])
        if mods:
            message = texts.RELAY_TO_MODS.format(
                section=sec["title"], children=names,
                parent=self.db.user_display(parent), text=text)
            for mod in mods:
                if mod["user_id"] != user_id:
                    self.send(mod["user_id"], message, buttons)
        else:
            message = texts.RELAY_TO_MODS.format(
                section=sec["title"], children=names,
                parent=self.db.user_display(parent), text=text)
            self.notify_supers(message, buttons)

    def _deliver_moderator_message(self, user_id: int, child_id: int, text: str) -> None:
        child = self.db.get_child(child_id)
        self.db.clear_state(user_id)
        if not child:
            self.send(user_id, texts.CANCELLED)
            return
        sec = self.db.get_section(child["section_id"])
        moderator = self.db.get_user(user_id)
        self.send(
            child["parent_id"],
            texts.WRITE_PARENT_DELIVERED.format(
                section=sec["title"] if sec else "?",
                moderator=self.db.user_display(moderator),
                text=text),
        )
        self.send(user_id, texts.WRITE_PARENT_CONFIRM.format(
            child=f"{child['last_name']} {child['first_name']}"))

    # ================================================================== #
    # Супер-модератор
    # ================================================================== #
    def _super_callback(
        self, callback_id: str, user_id: int, payload: str, parts: list[str]
    ) -> None:
        if not self.is_super(user_id):
            self.safe_answer(callback_id, notification="Недостаточно прав")
            return

        if payload == kb.P_OVERVIEW:
            text, buttons = self._super_overview()
            self.safe_answer(callback_id, text=text, buttons=buttons, fallback_user_id=user_id)

        elif payload == kb.P_ADD_SEC:
            self.db.set_state(user_id, "a_sec_title")
            self.safe_answer(
                callback_id,
                text=texts.ADD_SEC_ASK_TITLE,
                buttons=make_kb(kb.row(kb.btn("⬅️ Отмена", kb.P_CANCEL))),
                fallback_user_id=user_id,
            )

        elif payload == kb.P_DEL_SEC:
            text, buttons = self._pick_section(kb.P_DEL_SEC_ID, texts.DEL_SEC_PICK, kb.NEGATIVE)
            self.safe_answer(callback_id, text=text, buttons=buttons, fallback_user_id=user_id)

        elif parts[0] == "a" and parts[1] == "del":
            section_id = int(parts[2])
            sec = self.db.get_section(section_id)
            if not sec:
                self.safe_answer(callback_id, notification="Секция не найдена")
                return
            counts = self._section_counts(section_id)
            text = texts.DEL_SEC_CONFIRM.format(
                section=sec["title"], **counts)
            buttons = make_kb(
                kb.row(kb.btn("🗑 Да, удалить", kb.P_DEL_SEC_YES.format(section_id=section_id), kb.NEGATIVE)),
                kb.row(kb.btn("⬅️ Отмена", kb.P_CANCEL)),
            )
            self.safe_answer(callback_id, text=text, buttons=buttons, fallback_user_id=user_id)

        elif parts[0] == "a" and parts[1] == "delyes":
            section_id = int(parts[2])
            sec = self.db.get_section(section_id)
            if not sec:
                self.safe_answer(callback_id, notification="Секция не найдена")
                return
            self.db.delete_section(section_id)
            text, buttons = self._super_overview()
            self.safe_answer(
                callback_id,
                text=texts.DEL_SEC_DONE.format(section=sec["title"]) + "\n\n" + text,
                buttons=buttons,
                notification="Секция удалена",
                fallback_user_id=user_id,
            )

        elif payload == kb.P_MOD_SEC:
            text, buttons = self._pick_section(kb.P_MOD_CODE, texts.MOD_SEC_PICK)
            self.safe_answer(callback_id, text=text, buttons=buttons, fallback_user_id=user_id)

        elif parts[0] == "a" and parts[1] == "modcode":
            section_id = int(parts[2])
            sec = self.db.get_section(section_id)
            if not sec:
                self.safe_answer(callback_id, notification="Секция не найдена")
                return
            code = self.db.create_invite(section_id)
            self.safe_answer(
                callback_id,
                text=texts.MOD_SEC_CODE.format(section=sec["title"], code=code),
                buttons=make_kb(kb.back_row()),
                fallback_user_id=user_id,
            )

        elif payload == kb.P_UNMOD_SEC:
            text, buttons = self._pick_section(kb.P_UNMOD_SEC_ID, texts.UNMOD_SEC_PICK)
            self.safe_answer(callback_id, text=text, buttons=buttons, fallback_user_id=user_id)

        elif parts[0] == "a" and parts[1] == "unmod1":
            section_id = int(parts[2])
            sec = self.db.get_section(section_id)
            if not sec:
                self.safe_answer(callback_id, notification="Секция не найдена")
                return
            mods = self.db.section_moderators(section_id)
            if not mods:
                text = texts.MODS_LIST_EMPTY.format(section=sec["title"])
                buttons = make_kb(kb.back_row())
            else:
                rows = [
                    kb.row(kb.btn(
                        self.db.user_display(m),
                        kb.P_UNMOD_USER.format(section_id=section_id, user_id=m["user_id"]),
                        kb.NEGATIVE))
                    for m in mods
                ]
                rows.append(kb.back_row())
                text = texts.UNMOD_MOD_PICK.format(section=sec["title"])
                buttons = make_kb(*rows)
            self.safe_answer(callback_id, text=text, buttons=buttons, fallback_user_id=user_id)

        elif parts[0] == "a" and parts[1] == "unmod2":
            section_id, target_id = int(parts[2]), int(parts[3])
            sec = self.db.get_section(section_id)
            target = self.db.get_user(target_id)
            if not sec or not target:
                self.safe_answer(callback_id, notification="Не найдено")
                return
            self.db.remove_moderator(target_id, section_id)
            self.send(target_id, texts.UNMOD_NOTIFY_USER.format(section=sec["title"]))
            text, buttons = self._super_overview()
            self.safe_answer(
                callback_id,
                text=texts.UNMOD_DONE.format(
                    section=sec["title"], user=self.db.user_display(target))
                + "\n\n" + text,
                buttons=buttons,
                notification="Модератор снят",
                fallback_user_id=user_id,
            )
        else:
            self.safe_answer(callback_id, notification="Кнопка неактуальна — /menu")

    def _pick_section(self, payload_tpl: str, title: str, intent: str | None = None) -> tuple[str, list[list[dict]]]:
        sections = self.db.sections()
        if not sections:
            return texts.SUPER_NO_SECTIONS, make_kb(kb.back_row())
        rows = [
            kb.row(kb.btn(f"«{s['title']}»", payload_tpl.format(section_id=s["id"]), intent))
            for s in sections
        ]
        rows.append(kb.back_row())
        return title, make_kb(*rows)

    def _super_overview(self) -> tuple[str, list[list[dict]]]:
        sections = self.db.sections()
        if not sections:
            return texts.SUPER_NO_SECTIONS, make_kb(kb.back_row())
        lines = [texts.SUPER_OVERVIEW_TITLE]
        for sec in sections:
            mods = self.db.section_moderators(sec["id"])
            mods_line = ", ".join(self.db.user_display(m) for m in mods) if mods else "не назначены"
            children_all = [
                c for c in self.db.active_children_of_section(sec["id"])
            ]
            pending = len(self.db.pending_children_of_section(sec["id"]))
            price = f" ({int(sec['price'])} ₽/мес)" if sec["price"] else ""
            lines.append(texts.SUPER_OVERVIEW_SECTION.format(
                section=sec["title"], price=price, mods=mods_line,
                children=len(children_all), pending=pending))
        return "\n".join(lines), make_kb(kb.back_row())

    def _section_counts(self, section_id: int) -> dict[str, int]:
        """Число детей, платежей и модераторов секции (для подтверждения удаления)."""
        return self.db.section_counts(section_id)

    def _create_section_from_input(self, user_id: int, title: str, price_text: str) -> None:
        price_text = price_text.strip()
        price = None
        if price_text not in ("-", "–", "—", ""):
            try:
                price = float(price_text.replace(",", ".").replace(" ", ""))
                if price < 0:
                    raise ValueError
            except ValueError:
                self.send(user_id, texts.ADD_SEC_BAD_PRICE)
                return
        section_id = self.db.add_section(title, price)
        self.db.clear_state(user_id)
        sec = self.db.get_section(section_id)
        price_line = f" ({int(price)} ₽/мес)" if price else ""
        self.send(user_id, texts.ADD_SEC_DONE.format(section=title, price=price_line))
        # обновляем главное меню
        menu_text, menu_kb = self.main_menu(user_id)
        self.send(user_id, menu_text, menu_kb)

    def _claim_invite(self, user_id: int, code: str) -> None:
        section_id = self.db.use_invite(code)
        if section_id is None:
            self.send(user_id, texts.MOD_CLAIM_BAD.format(code=code))
            return
        sec = self.db.get_section(section_id)
        if not sec:
            self.send(user_id, texts.MOD_CLAIM_BAD.format(code=code))
            return
        self.db.add_moderator(user_id, section_id)
        user = self.db.get_user(user_id)
        self.send(user_id, texts.MOD_CLAIM_OK.format(section=sec["title"]))
        self.notify_supers(texts.MOD_CLAIM_NOTIFY.format(
            section=sec["title"], user=self.db.user_display(user)))
        # новое меню с секцией
        menu_text, menu_kb = self.main_menu(user_id)
        self.send(user_id, menu_text, menu_kb)

    # ================================================================== #
    # Excel
    # ================================================================== #
    def _send_excel(self, user_id: int, sec) -> None:
        try:
            import excel_export

            path = excel_export.build_workbook(self.db, sec)
            self.client.send_file(
                user_id, path, caption=texts.EXCEL_CAPTION.format(section=sec["title"]))
        except Exception:  # noqa: BLE001
            log.exception("Ошибка выгрузки Excel")
            self.send(user_id, "⚠️ Не удалось сформировать Excel-файл. Попробуйте позже.")

    # ================================================================== #
    # Вспомогательное
    # ================================================================== #
    @staticmethod
    def _normalize_name(text: str) -> str | None:
        name = re.sub(r"\s+", " ", text).strip()
        if NAME_RE.match(name):
            return name
        return None
