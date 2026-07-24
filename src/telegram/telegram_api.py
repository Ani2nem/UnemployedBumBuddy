"""Thin wrapper around the Telegram Bot API used by every Lambda in this package."""

from __future__ import annotations

from typing import Any

import httpx

from src.telegram import config

_TIMEOUT = httpx.Timeout(10.0)


def _post(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{config.TELEGRAM_API_BASE_URL}/{method}"
    response = httpx.post(url, json=payload, timeout=_TIMEOUT)
    response.raise_for_status()
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram API {method} failed: {body}")
    return body["result"]


def approval_keyboard(job_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"approve:{job_id}"},
                {"text": "❌ Deny", "callback_data": f"deny:{job_id}"},
                {"text": "✏️ Edit", "callback_data": f"edit:{job_id}"},
            ]
        ]
    }


def send_message(
    chat_id: int | str,
    text: str,
    reply_markup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text[: config.TELEGRAM_MESSAGE_LIMIT]}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return _post("sendMessage", payload)


def edit_message_text(
    chat_id: int | str,
    message_id: int,
    text: str,
    reply_markup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text[: config.TELEGRAM_MESSAGE_LIMIT],
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return _post("editMessageText", payload)


def answer_callback_query(callback_query_id: str, text: str | None = None, alert: bool = False) -> None:
    payload: dict[str, Any] = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
        payload["show_alert"] = alert
    _post("answerCallbackQuery", payload)
