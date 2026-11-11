# -*- coding: utf-8 -*-
"""Интеграционные тесты бота без обращения к реальному MAX API.

FakeClient подменяет транспорт и записывает все исходящие сообщения.
Запуск:  python tests/test_bot.py   (или python -m unittest tests.test_bot)
"""

from __future__ import annotations

import itertools
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import utils  # noqa: E402
from db import Database  # noqa: E402
from handlers import BotApp  # noqa: E402

SUPER = 1001
MOD = 2002
PARENT = 3003
PARENT2 = 4004
STRANGER = 5555

USERS = {
    SUPER: ("Ольга", "Смирнова", "olga_admin"),
    MOD: ("Мария", "Петрова", "maria_mod"),
    PARENT: ("Иван", "Иванов", "ivan_parent"),
    PARENT2: ("Глеб", "Сидоров", "gleb"),
    STRANGER: ("Прохожий", "", ""),
}


class FakeClient:
    """Фейковый транспорт: пишет все сообщения в outbox."""

    def __init__(self):
        self.outbox: list[dict] = []
        self._presser: int | None = None

    def send_text(self, user_id, text, buttons=None, notify=True, *, chat=False):
        self.outbox.append(
            {"via": "text", "to": user_id, "text": text, "buttons": buttons,
             "chat": chat}
        )

    def answer_callback(self, callback_id, *, text=None, buttons=None,
                        notification=None):
        self.outbox.append(
            {"via": "answer", "to": self._presser, "text": text,
             "buttons": buttons, "notification": notification, "cb": callback_id}
        )

    def send_file(self, user_id, file_path, caption=None):
        self.outbox.append(
            {"via": "file", "to": user_id, "file": str(file_path), "caption": caption}
        )

    # --- helpers для тестов ---
    def to(self, user_id: int) -> list[dict]:
        return [m for m in self.outbox if m["to"] == user_id]

    def last(self, user_id: int) -> dict:
        msgs = self.to(user_id)
        assert msgs, f"нет сообщений пользователю {user_id}"
        return msgs[-1]

    def last_text(self, user_id: int) -> str:
        return str(self.last(user_id)["text"])

    def buttons_of(self, user_id: int) -> list[dict]:
        btns = []
        for row in self.last(user_id).get("buttons") or []:
            btns.extend(row)
        return btns

    def find_button(self, user_id: int, prefix: str) -> dict:
        for b in self.buttons_of(user_id):
            if str(b.get("payload", "")).startswith(prefix):
                return b
        raise AssertionError(
            f"у {user_id} нет кнопки с payload, начинающимся на {prefix!r}; "
            f"есть: {[b.get('payload') for b in self.buttons_of(user_id)]}"
        )

    def press(self, app: BotApp, user_id: int, payload: str) -> None:
        """Симулирует нажатие кнопки пользователем."""
        cb_id = f"cb{len(self.outbox)}"
        self._presser = user_id
        try:
            app.handle_update({
                "update_type": "message_callback",
                "message": None,
                "callback": {
                    "callback_id": cb_id,
                    "payload": payload,
                    "timestamp": 0,
                    "user": user_dict(user_id),
                },
            })
        finally:
            self._presser = None


def user_dict(user_id: int) -> dict:
    first, last, username = USERS[user_id]
    return {
        "user_id": user_id,
        "first_name": first,
        "last_name": last,
        "username": username,
        "is_bot": False,
    }


class BotTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / "test.db"))
        self.client = FakeClient()
        self.app = BotApp(self.client, self.db, bot_name="TestBot")
        self.app.super_ids = {SUPER}
        # все пользователи уже «знакомы» боту (как будто писали ему раньше)
        for uid, (first, last, username) in USERS.items():
            self.db.upsert_user(uid, first, last, username)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    # ------------------------------------------------------------------ #
    # Вспомогательные действия
    # ------------------------------------------------------------------ #
    def got(self, user_id: int, substr: str) -> bool:
        """Получал ли пользователь сообщение, содержащее подстроку."""
        return any(
            substr in str(m.get("text") or "")
            for m in self.client.to(user_id)
        )
    def msg(self, user_id: int, text: str) -> None:
        self.app.handle_update({
            "update_type": "message_created",
            "user_locale": "ru",
            "message": {
                "sender": user_dict(user_id),
                "recipient": {"chat_id": user_id, "chat_type": "dialog",
                              "user_id": user_id},
                "body": {"mid": "m1", "seq": 1, "text": text,
                         "attachments": []},
                "timestamp": 0,
            },
        })

    def press(self, user_id: int, payload: str) -> None:
        self.client.press(self.app, user_id, payload)

    def register_child(self, user_id: int, last: str, first: str,
                       section_id: int) -> None:
        """Быстрая регистрация ребёнка через штатный диалог."""
        self.press(user_id, "p:add")
        self.msg(user_id, last)
        self.msg(user_id, first)
        self.press(user_id, f"p:regsec:{section_id}")

    # ------------------------------------------------------------------ #
    # Тесты
    # ------------------------------------------------------------------ #
    def test_super_creates_sections(self):
        self.msg(SUPER, "/start")
        self.assertIn("Супер-модератор", self.client.last_text(SUPER))

        # Создание секции «Футбол» с ценой
        self.press(SUPER, "a:addsec")
        self.msg(SUPER, "Футбол")
        self.msg(SUPER, "3000")
        self.assertTrue(self.got(SUPER, "«Футбол»"), "нет подтверждения создания секции")
        sections = self.db.sections()
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0]["title"], "Футбол")
        self.assertEqual(sections[0]["price"], 3000.0)

        # Секция без фиксированной цены
        self.press(SUPER, "a:addsec")
        self.msg(SUPER, "Шахматы")
        self.msg(SUPER, "-")
        sections = self.db.sections()
        self.assertEqual(len(sections), 2)

        # Обзор
        self.press(SUPER, "a:over")
        text = self.client.last_text(SUPER)
        self.assertIn("Футбол", text)
        self.assertIn("Шахматы", text)
        self.assertIn("не назначены", text)

    def test_invite_and_claim_moderator(self):
        self.db.add_section("Футбол", 3000)
        self.press(SUPER, "a:modsec")
        self.press(SUPER, "a:modcode:1")
        code_m = re.search(r"MOD-\d{4}", self.client.last_text(SUPER))
        self.assertIsNotNone(code_m, "код приглашения не показан")
        code = code_m.group(0)

        # Будущий модератор вводит код
        self.msg(MOD, code.lower())  # регистр не важен
        self.assertTrue(self.got(MOD, "Вы теперь модератор секции"))
        self.assertTrue(self.db.is_moderator(MOD, 1))
        # Супер получил уведомление
        self.assertTrue(self.got(SUPER, "Новый модератор"))

        # Повторное использование кода невозможно
        self.msg(PARENT, code)
        self.assertTrue(self.got(PARENT, "недействителен"))
        self.assertFalse(self.db.is_moderator(PARENT, 1))

    def test_application_flow(self):
        sec = self.db.add_section("Футбол", 3000)
        self.db.add_moderator(MOD, sec)

        # Родитель регистрирует ребёнка
        self.msg(PARENT, "/start")
        self.assertIn("фамилию", self.client.last_text(PARENT))
        self.msg(PARENT, "Иванов")
        self.assertIn("имя", self.client.last_text(PARENT))
        self.msg(PARENT, "Иван")
        self.assertIn("Выберите секцию", self.client.last_text(PARENT))
        self.press(PARENT, f"p:regsec:{sec}")
        self.assertIn("Заявка отправлена", self.client.last_text(PARENT))

        # Модератор видит заявку с кнопками
        self.assertIn("Новая заявка", self.client.last_text(MOD))
        self.assertIsNotNone(self.db.pending_children_of_section(sec))

        # Принятие
        self.press(MOD, f"apps:{sec}")
        self.assertIn("Иванов Иван", self.client.last_text(MOD))
        ok_btn = self.client.find_button(MOD, f"app:")
        self.press(MOD, ok_btn["payload"])
        self.assertIn("принята", self.client.last_text(PARENT))
        children = self.db.active_children_of_section(sec)
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]["status"], "active")

        # Отклонение второй заявки
        self.msg(PARENT2, "/start")
        self.msg(PARENT2, "Сидоров")
        self.msg(PARENT2, "Сидор")
        self.press(PARENT2, f"p:regsec:{sec}")
        self.press(MOD, f"apps:{sec}")
        declined_child = self.db.pending_children_of_section(sec)[0]
        self.press(MOD, f"app:{declined_child['id']}:no")
        self.assertIn("отклонена", self.client.last_text(PARENT2))
        self.assertEqual(len(self.db.active_children_of_section(sec)), 1)
        declined = [c for c in self.db.children_of(PARENT2)
                    if c["status"] == "declined"]
        self.assertEqual(len(declined), 1)

    def test_duplicate_child_rejected(self):
        sec = self.db.add_section("Футбол", 3000)
        self.db.add_moderator(MOD, sec)
        self.register_child(PARENT, "Иванов", "Иван", sec)
        pending = self.db.pending_children_of_section(sec)
        self.press(MOD, f"app:{pending[0]['id']}:ok")
        # Повторная запись того же ребёнка
        self.register_child(PARENT, "Иванов", "Иван", sec)
        self.assertIn("уже записан", self.client.last_text(PARENT))
        self.assertEqual(len(self.db.children_of(PARENT)), 1)

    def test_payments_table_and_status(self):
        sec = self.db.add_section("Футбол", 3000)
        self.db.add_moderator(MOD, sec)
        self.register_child(PARENT, "Иванов", "Иван", sec)
        self.register_child(PARENT2, "Петров", "Пётр", sec)
        # принимаем обе заявки
        for _ in range(2):
            pending = self.db.pending_children_of_section(sec)
            if not pending:
                break
            self.press(MOD, f"app:{pending[0]['id']}:ok")

        period = utils.current_period()
        # Таблица
        self.press(MOD, f"s:{sec}")
        self.assertIn("Секция «Футбол»", self.client.last_text(MOD))
        self.press(MOD, f"t:{sec}:{period}")
        table = self.client.last_text(MOD)
        self.assertIn("0 из 2", table)
        # Выбор ребёнка и отметка оплаты
        self.press(MOD, f"tp:{sec}:{period}:0")
        ivanov = [c for c in self.db.active_children_of_section(sec)
                  if c["last_name"] == "Иванов"][0]
        self.press(MOD, f"c:{sec}:{period}:{ivanov['id']}:0")
        self.assertIn("не оплачено", self.client.last_text(MOD))
        self.press(MOD, f"pay:{ivanov['id']}:{period}:1")
        self.assertIn("оплачено", self.client.last_text(MOD))
        self.assertIsNotNone(self.db.is_paid(ivanov["id"], period))

        # Таблица обновилась
        self.press(MOD, f"t:{sec}:{period}")
        self.assertIn("1 из 2", self.client.last_text(MOD))

        # Родитель видит свой статус
        self.press(PARENT, "p:status")
        status = self.client.last_text(PARENT)
        self.assertIn("Иванов Иван", status)
        self.assertIn(utils.period_title(period), status)
        # Снятие оплаты
        self.press(MOD, f"pay:{ivanov['id']}:{period}:0")
        self.assertIsNone(self.db.is_paid(ivanov["id"], period))

    def test_permissions(self):
        sec = self.db.add_section("Футбол", 3000)
        # Посторонний не может открыть таблицу
        self.press(STRANGER, f"t:{sec}:{utils.current_period()}")
        self.assertEqual(self.client.last(STRANGER)["notification"],
                         "Недостаточно прав")
        # И отметить оплату
        self.press(STRANGER, f"pay:1:{utils.current_period()}:1")
        self.assertEqual(self.client.last(STRANGER)["notification"],
                         "Недостаточно прав")
        # А супер — может
        self.press(SUPER, f"t:{sec}:{utils.current_period()}")
        self.assertIn("Футбол", self.client.last_text(SUPER))

    def test_relay_and_reply(self):
        sec = self.db.add_section("Футбол", 3000)
        self.db.add_moderator(MOD, sec)
        self.register_child(PARENT, "Иванов", "Иван", sec)
        pending = self.db.pending_children_of_section(sec)
        self.press(MOD, f"app:{pending[0]['id']}:ok")

        # Родитель пишет свободным текстом -> релей модераторам
        self.msg(PARENT, "Здравствуйте! Когда ближайшее занятие?")
        relay = self.client.last_text(MOD)
        self.assertIn("Сообщение от родителя", relay)
        self.assertIn("Иванов Иван", relay)
        reply_btn = self.client.find_button(MOD, "mwr:")
        self.assertIn("Ответить", reply_btn["text"])
        self.assertIn("передано модераторам", self.client.last_text(PARENT))

        # Модератор отвечает через кнопку
        self.press(MOD, reply_btn["payload"])
        self.assertIn("Напишите сообщение", self.client.last_text(MOD))
        self.msg(MOD, "В среду в 18:00!")
        delivered = self.client.last_text(PARENT)
        self.assertIn("В среду в 18:00", delivered)
        self.assertIn("Сообщение от модератора", delivered)

        # Родитель пишет через кнопку «Написать модератору»
        self.press(PARENT, "p:write")
        self.assertIn("Напишите сообщение", self.client.last_text(PARENT))
        self.msg(PARENT, "Спасибо!")
        self.assertIn("Сообщение от родителя", self.client.last_text(MOD))

    def test_contacts_view(self):
        sec = self.db.add_section("Футбол", 3000)
        self.db.add_moderator(MOD, sec)
        self.register_child(PARENT, "Иванов", "Иван", sec)
        self.press(PARENT, "p:contacts")
        contacts = self.client.last_text(PARENT)
        self.assertIn("Мария Петрова (@maria_mod)", contacts)

    def test_manual_reminders(self):
        sec = self.db.add_section("Футбол", 3000)
        self.db.add_moderator(MOD, sec)
        self.register_child(PARENT, "Иванов", "Иван", sec)
        pending = self.db.pending_children_of_section(sec)
        self.press(MOD, f"app:{pending[0]['id']}:ok")
        self.press(MOD, f"rem:{sec}")
        self.assertIn("Напоминания", self.client.last_text(MOD))
        reminder = self.client.last_text(PARENT)
        self.assertIn("Напоминание об оплате", reminder)
        self.assertIn("Иванов Иван", reminder)

    def test_auto_reminders(self):
        from reminders import ReminderScheduler

        sec = self.db.add_section("Футбол", 3000)
        self.db.add_moderator(MOD, sec)
        self.register_child(PARENT, "Иванов", "Иван", sec)
        pending = self.db.pending_children_of_section(sec)
        self.press(MOD, f"app:{pending[0]['id']}:ok")

        scheduler = ReminderScheduler(self.app)
        period = utils.current_period()
        scheduler.send_auto_reminders(period)
        # родителю ушло напоминание
        self.assertIn("Напоминание об оплате", self.client.last_text(PARENT))
        # супер получил сводку
        self.assertIn("Автоматические напоминания", self.client.last_text(SUPER))

    def test_excel_export(self):
        sec = self.db.add_section("Футбол", 3000)
        self.db.add_moderator(MOD, sec)
        self.register_child(PARENT, "Иванов", "Иван", sec)
        pending = self.db.pending_children_of_section(sec)
        self.press(MOD, f"app:{pending[0]['id']}:ok")
        period = utils.current_period()
        ivanov = self.db.active_children_of_section(sec)[0]
        self.db.mark_paid(ivanov["id"], period, 3000, MOD)

        self.press(MOD, f"xls:{sec}")
        sent = [m for m in self.client.outbox if m["via"] == "file"]
        self.assertEqual(len(sent), 1)
        path = Path(sent[0]["file"])
        self.assertTrue(path.exists())

        from openpyxl import load_workbook

        wb = load_workbook(path)
        self.assertEqual(wb.sheetnames, ["Сводная", "Платежи"])
        ws = wb["Сводная"]
        values = [[c.value for c in row] for row in ws.iter_rows()]
        flat = [v for row in values for v in row if v]
        self.assertIn("Иванов", flat)
        self.assertIn("Иван", flat)
        self.assertIn(utils.period_title(period), flat)
        wp = wb["Платежи"]
        pay_rows = [[c.value for c in row] for row in wp.iter_rows(min_row=2)]
        self.assertEqual(len(pay_rows), 1)
        self.assertEqual(pay_rows[0][1], "Иванов")

    def test_remove_moderator(self):
        sec = self.db.add_section("Футбол", 3000)
        self.db.add_moderator(MOD, sec)
        self.press(SUPER, "a:unmod")
        self.press(SUPER, f"a:unmod1:{sec}")
        self.press(SUPER, f"a:unmod2:{sec}:{MOD}")
        self.assertFalse(self.db.is_moderator(MOD, sec))
        self.assertIn("больше не модератор", self.client.last_text(SUPER))
        self.assertIn("больше не модератор", self.client.last_text(MOD))

    def test_delete_section(self):
        sec = self.db.add_section("Футбол", 3000)
        self.db.add_moderator(MOD, sec)
        self.register_child(PARENT, "Иванов", "Иван", sec)
        child = self.db.pending_children_of_section(sec)[0]
        self.db.set_child_status(child["id"], "active")
        self.db.mark_paid(child["id"], utils.current_period(), 3000, MOD)

        self.press(SUPER, "a:delsec")
        self.press(SUPER, f"a:del:{sec}")
        confirm = self.client.last_text(SUPER)
        self.assertIn("Удалить секцию", confirm)
        self.assertIn("детей: 1", confirm)
        self.press(SUPER, f"a:delyes:{sec}")
        self.assertEqual(self.db.sections(), [])
        self.assertIsNone(self.db.get_section(sec))
        self.assertEqual(self.db.section_counts(sec)["children"], 0)

    def test_no_sections_registration(self):
        self.msg(PARENT, "/start")
        # бот предложит регистрацию, но секций нет
        self.msg(PARENT, "Иванов")
        self.msg(PARENT, "Иван")
        self.assertIn("не создано ни одной секции", self.client.last_text(PARENT))

    def test_group_chat_ignored(self):
        self.app.handle_update({
            "update_type": "message_created",
            "message": {
                "sender": user_dict(PARENT),
                "recipient": {"chat_id": 777, "chat_type": "chat"},
                "body": {"mid": "m", "seq": 1, "text": "/start"},
                "timestamp": 0,
            },
        })
        self.assertEqual(self.client.to(PARENT), [])

    def test_bot_started_variants(self):
        # вариант А: в user пришёл человек
        self.app.handle_update({
            "update_type": "bot_started",
            "chat_id": 500001,
            "user": user_dict(PARENT),
            "timestamp": 0,
        })
        self.assertIn("Нажмите «Начать»", self.client.last_text(PARENT))

        # вариант Б: в user пришёл сам бот (is_bot=True) — пишем по chat_id
        self.app.handle_update({
            "update_type": "bot_started",
            "chat_id": 700001,
            "user": {"user_id": 999, "name": "TestBot", "is_bot": True},
            "timestamp": 0,
        })
        last = self.client.last(700001)
        self.assertTrue(last["chat"], "приветствие должно уйти по chat_id")
        self.assertIn("Нажмите «Начать»", str(last["text"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
