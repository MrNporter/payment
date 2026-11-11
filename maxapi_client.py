# -*- coding: utf-8 -*-
"""Тонкий клиент MAX Bot API поверх requests.

Реализует только то, что нужно боту:
  - long polling обновлений (GET /updates);
  - отправка сообщений с inline-клавиатурой (POST /messages);
  - ответы на нажатия кнопок (POST /answers);
  - загрузка и отправка файлов (POST /uploads + POST /messages).

Форматы запросов соответствуют официальной документации
https://dev.max.ru/docs-api и проверены по исходникам официальной
python-библиотеки maxapi (версия 1.2.4).
"""

from __future__ import annotations

import json
import logging
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import certifi
import requests

log = logging.getLogger("maxapi")

#: Максимальная длина текста сообщения в MAX.
MAX_TEXT_LEN = 4000
#: Запас на служебные обёртки при разбиении длинного текста.
CHUNK_LEN = 3200


class MaxApiError(Exception):
    """Ошибка ответа MAX API."""

    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"MAX API error {status}: {body[:300]}")


class InvalidTokenError(MaxApiError):
    """Токен бота неверен или отозван (HTTP 401)."""


_ca_bundle_cache: "str | bool | None" = None


def _ca_bundle_path() -> "str | bool":
    """Собирает CA-бандл: certifi + российский НУЦ (Минцифры).

    Серверы MAX используют сертификаты российского удостоверяющего
    центра, которого может не быть в системном хранилище. Возвращаем
    путь к временному файлу с объединённым набором корней.
    Результат кэшируется на процесс: временный файл создаётся один раз.
    """
    global _ca_bundle_cache
    if _ca_bundle_cache is not None:
        return _ca_bundle_cache

    pem_ru = Path(__file__).resolve().parent / "ca" / "russiantrustedca.pem"
    try:
        parts = [Path(certifi.where()).read_text(encoding="utf-8")]
        if pem_ru.exists():
            parts.append(pem_ru.read_text(encoding="utf-8"))
        bundle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".pem", delete=False, encoding="utf-8"
        )
        bundle.write("\n".join(parts))
        bundle.close()
        _ca_bundle_cache = bundle.name
    except Exception:  # noqa: BLE001 — при любой проблеме откатываемся на обычную проверку
        _ca_bundle_cache = True
    return _ca_bundle_cache


def split_text(text: str, limit: int = CHUNK_LEN) -> list[str]:
    """Делит длинный текст на части по границам строк (лимит MAX — 4000)."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            parts.append(current)
            current = line
        else:
            current = line if not current else current + "\n" + line
    if current:
        parts.append(current)
    return parts


class MaxClient:
    """Клиент MAX Bot API. Все методы блокирующие (requests)."""

    def __init__(self, token: str, base_url: str, poll_timeout: int = 30):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.poll_timeout = poll_timeout
        self.session = requests.Session()
        self.session.headers.update({"Authorization": self.token})
        self.session.verify = _ca_bundle_path()
        self.marker: int | None = None

    # ------------------------------------------------------------------ #
    # Низкоуровневые запросы
    # ------------------------------------------------------------------ #
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: int = 60,
    ) -> Any:
        url = self.base_url + path
        resp = self.session.request(
            method, url, params=params, json=json_body, timeout=timeout
        )
        if resp.status_code == 401:
            raise InvalidTokenError(401, resp.text)
        if resp.status_code >= 400:
            raise MaxApiError(resp.status_code, resp.text)
        if not resp.text.strip():
            return {}
        return resp.json()

    # ------------------------------------------------------------------ #
    # Публичные методы API
    # ------------------------------------------------------------------ #
    def get_me(self) -> dict[str, Any]:
        """Информация о боте (GET /me). Используется для проверки токена."""
        return self._request("GET", "/me")

    def get_updates(self) -> dict[str, Any]:
        """Long polling: GET /updates. Возвращает {'updates': [...], 'marker': N}."""
        params: dict[str, Any] = {
            "timeout": self.poll_timeout,
            "limit": 100,
            "types": "message_created,message_callback,bot_started",
        }
        if self.marker is not None:
            params["marker"] = self.marker
        data = self._request(
            "GET", "/updates", params=params, timeout=self.poll_timeout + 60
        )
        if isinstance(data, dict):
            self.marker = data.get("marker", self.marker)
        return data

    def send_text(
        self,
        user_id: int,
        text: str,
        buttons: list[list[dict[str, Any]]] | None = None,
        notify: bool = True,
        *,
        chat: bool = False,
    ) -> None:
        """Отправляет текстовое сообщение пользователю (личный диалог).

        По умолчанию адресат — user_id; при chat=True считаем, что в
        user_id передан chat_id (так приходит в обновлении bot_started).
        Длинные сообщения автоматически разбиваются на части;
        клавиатура добавляется только к последней части.
        """
        parts = split_text(text)
        total = len(parts)
        for i, chunk in enumerate(parts):
            body: dict[str, Any] = {"text": chunk, "notify": notify}
            if buttons is not None and i == total - 1:
                body["attachments"] = [
                    {"type": "inline_keyboard", "payload": {"buttons": buttons}}
                ]
            key = "chat_id" if chat else "user_id"
            self._request(
                "POST", "/messages", params={key: user_id}, json_body=body
            )

    def answer_callback(
        self,
        callback_id: str,
        *,
        text: str | None = None,
        buttons: list[list[dict[str, Any]]] | None = None,
        notification: str | None = None,
    ) -> None:
        """Отвечает на нажатие кнопки: обновляет сообщение и/или
        показывает всплывающее уведомление (POST /answers)."""
        body: dict[str, Any] = {}
        if text is not None or buttons is not None:
            message: dict[str, Any] = {"notify": True}
            if text is not None:
                message["text"] = text
            if buttons is not None:
                message["attachments"] = [
                    {
                        "type": "inline_keyboard",
                        "payload": {"buttons": buttons},
                    }
                ]
            body["message"] = message
        if notification:
            body["notification"] = notification
        self._request(
            "POST",
            "/answers",
            params={"callback_id": callback_id},
            json_body=body,
        )

    def send_file(
        self, user_id: int, file_path: str | Path, caption: str | None = None
    ) -> None:
        """Загружает файл на сервер MAX и отправляет его пользователю.

        Схема (POST /uploads -> upload-URL -> token -> POST /messages).
        """
        file_path = Path(file_path)
        info = self._request("POST", "/uploads", params={"type": "file"})
        upload_url = info.get("url")
        if not upload_url:
            raise MaxApiError(0, "Пустой upload url: " + json.dumps(info))
        with file_path.open("rb") as fh:
            files = {"data": (file_path.name, fh)}
            # upload-URL подписан токеном; заголовок авторизации не нужен,
            # но на случай строгого шлюза пробуем оба варианта.
            resp = requests.post(upload_url, files=files, timeout=120)
            if resp.status_code >= 400:
                resp = self.session.post(upload_url, files=files, timeout=120)
        if resp.status_code >= 400:
            raise MaxApiError(resp.status_code, resp.text)
        payload = resp.json()
        token = payload.get("token")
        if not token:
            raise MaxApiError(0, "Нет token в ответе upload-сервера")
        body: dict[str, Any] = {
            "attachments": [{"type": "file", "payload": {"token": token}}],
            "notify": True,
        }
        if caption:
            body["text"] = caption
        self._request(
            "POST", "/messages", params={"user_id": user_id}, json_body=body
        )


def retry(func: Callable[[], Any], attempts: int = 5, base_delay: float = 2.0) -> Any:
    """Повторяет вызов при сетевых/серверных ошибках с нарастающей паузой.

    Ошибка неверного токена не повторяется — пробрасывается сразу.
    """
    for attempt in range(attempts):
        try:
            return func()
        except (requests.RequestException, MaxApiError) as exc:
            if isinstance(exc, InvalidTokenError):
                raise
            # Повторяем только сетевые сбои и ошибки сервера (5xx),
            # клиентские ошибки (4xx) бессмысленно повторять.
            transient = not isinstance(exc, MaxApiError) or exc.status >= 500
            if not transient or attempt == attempts - 1:
                raise
            delay = base_delay * (attempt + 1)
            log.warning("Сетевая ошибка %s, повтор через %.0fс", exc, delay)
            time.sleep(delay)
    raise RuntimeError("unreachable")
