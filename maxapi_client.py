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
import os
import socket
import ssl
from urllib.parse import urlparse
def _build_trusted_bundle(base_url: str) -> str:
    """Собирает CA-бандл: certifi + российский корень (если валиден)
    + сертификат сервера через TLS без проверки (TOFU, гарантированный fallback).

    Возвращает путь к итоговому PEM-файлу. Логирует каждый шаг.
    """
    log.info("CA: === старт сборки бандла ===")

    parts: list[str] = []

    # 1) certifi — базовый набор западных корней
    certifi_path = Path(certifi.where())
    parts.append(certifi_path.read_text(encoding="utf-8"))
    log.info("CA: certifi %s (%d байт)", certifi_path, certifi_path.stat().st_size)

    # 2) Российский корень — только если файл реально PEM
    ca_dir = Path(__file__).resolve().parent / "ca"
    log.info("CA: каталог %s существует=%s", ca_dir, ca_dir.exists())
    for name in ("russiantrustedca.pem", "russiantrustedsubca.pem"):
        p = ca_dir / name
        if not p.exists():
            log.warning("CA: %s не найден", p)
            continue
        try:
            raw = p.read_bytes()
            text = raw.decode("utf-8", errors="replace")
            log.info(
                "CA: %s size=%d first=%r last=%r",
                name, len(raw),
                text[:40] if text else "",
                text[-40:] if text else "",
            )
            if "BEGIN CERTIFICATE" not in text:
                log.error("CA: %s не PEM — пропускаем", name)
                continue
            parts.append(text)
            log.info("CA: добавлен %s", name)
        except Exception as exc:  # noqa: BLE001
            log.exception("CA: ошибка чтения %s: %s", name, exc)

    # 3) TOFU: сертификат сервера без верификации
    host = urlparse(base_url).hostname or "platform-api2.max.ru"
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, 443), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                der = ssock.getpeercert(binary_form=True)
                peer_pem = ssl.DER_cert_to_PEM_cert(der)
                parts.append(peer_pem)
                log.info("CA: добавлен peer cert хоста %s", host)
    except Exception as exc:  # noqa: BLE001
        log.exception("CA: не удалось получить peer cert %s: %s", host, exc)

    bundle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".pem", delete=False, encoding="utf-8"
    )
    bundle.write("\n".join(parts))
    bundle.close()
    log.info(
        "CA: bundle %s (%d блоков, %d байт)",
        bundle.name, len(parts), os.path.getsize(bundle.name),
    )
    return bundle.name

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
        self.session.verify = _build_trusted_bundle(base_url)
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
    "types": ["message_created", "message_callback", "bot_started"],
}
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
