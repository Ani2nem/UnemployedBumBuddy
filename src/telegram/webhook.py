"""API Gateway webhook entrypoint for the Telegram bot.

Registered as the bot's webhook URL. Handles two paths:

  - callback_query: Approve/Deny/Edit inline keyboard taps. Resolved against
    PendingApprovals and resumed via Step Functions SendTaskSuccess/Failure.
    "Edit" doesn't resume anything itself - it flags the chat as awaiting a
    follow-up free-text message with the edit instructions.
  - free-text message: either the edit instructions for a job flagged above
    (bumps revision_count, bounded retries), or - if there's no pending-edit
    context - an ad hoc question pushed to SQS for qa_worker to answer.

Must always return fast: API Gateway HTTP APIs time out at 29s, and Telegram
retries on anything but a prompt 2xx. No Serper/Bedrock calls happen inline
here - see qa_worker.py for that.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
from typing import Any

import boto3

from telegram import config, step_functions, store, telegram_api

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_sqs = boto3.client("sqs")

_RESEARCHING_TEXT = "🔍 Researching your question, one moment..."
_CLEARED_KEYBOARD: dict[str, Any] = {"inline_keyboard": []}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if not _is_authentic(event):
        return _response(401, "unauthorized")

    try:
        body = _parse_body(event)
    except (ValueError, json.JSONDecodeError):
        logger.warning("Malformed webhook body")
        return _response(400, "malformed body")

    if "callback_query" in body:
        _handle_callback_query(body["callback_query"])
    elif "message" in body and "text" in body["message"]:
        _handle_text_message(body["message"])

    return _response(200, "ok")


def _parse_body(event: dict[str, Any]) -> dict[str, Any]:
    raw_body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode("utf-8")
    return json.loads(raw_body)


def _is_authentic(event: dict[str, Any]) -> bool:
    if not config.TELEGRAM_WEBHOOK_SECRET:
        return True
    headers = event.get("headers") or {}
    provided = headers.get("x-telegram-bot-api-secret-token", "")
    return hmac.compare_digest(provided, config.TELEGRAM_WEBHOOK_SECRET)


def _response(status_code: int, message: str) -> dict[str, Any]:
    return {"statusCode": status_code, "body": json.dumps({"message": message})}


def _with_status(pending: dict[str, Any], status_line: str) -> str:
    return f"{pending['brief_text']}\n\n{pending['draft_text']}\n\n{status_line}"


def _handle_callback_query(callback_query: dict[str, Any]) -> None:
    callback_id = callback_query["id"]
    data = callback_query.get("data", "")
    action, _, job_id = data.partition(":")
    chat_id = callback_query["message"]["chat"]["id"]
    message_id = callback_query["message"]["message_id"]

    pending = store.get_pending_approval(job_id)
    if pending is None:
        telegram_api.answer_callback_query(
            callback_id, "This request has expired or was already handled.", alert=True
        )
        return

    if action == "approve":
        _resolve(pending, job_id, chat_id, message_id, callback_id, "approve", "✅ Approved", "Approved")
    elif action == "deny":
        _resolve(pending, job_id, chat_id, message_id, callback_id, "deny", "❌ Denied", "Denied")
    elif action == "edit":
        store.set_awaiting_edit(chat_id, job_id)
        telegram_api.edit_message_text(
            chat_id,
            message_id,
            _with_status(pending, "✏️ Send your edit instructions as your next message."),
            reply_markup=_CLEARED_KEYBOARD,
        )
        telegram_api.answer_callback_query(callback_id, "Send your edit instructions")
    else:
        logger.warning("Unknown callback action %r for job %s", action, job_id)
        telegram_api.answer_callback_query(callback_id, "Unrecognized action.", alert=True)


def _resolve(
    pending: dict[str, Any],
    job_id: str,
    chat_id: int,
    message_id: int,
    callback_id: str,
    action: str,
    status_line: str,
    ack_text: str,
) -> None:
    step_functions.send_task_success(pending["task_token"], {"decision": action, "edit_feedback": None})
    store.delete_pending_approval(job_id)
    store.clear_awaiting_edit_if_matches(chat_id, job_id)
    telegram_api.edit_message_text(
        chat_id, message_id, _with_status(pending, status_line), reply_markup=_CLEARED_KEYBOARD
    )
    telegram_api.answer_callback_query(callback_id, ack_text)


def _handle_text_message(message: dict[str, Any]) -> None:
    chat_id = message["chat"]["id"]
    text = message["text"]

    conversation_state = store.get_conversation_state(chat_id)
    awaiting_job_id = conversation_state.get("awaiting_edit_job_id")

    if awaiting_job_id:
        _handle_edit_feedback(chat_id, awaiting_job_id, text)
    else:
        _handle_ad_hoc_question(chat_id, text)


def _handle_edit_feedback(chat_id: int, job_id: str, feedback_text: str) -> None:
    pending = store.get_pending_approval(job_id)
    if pending is None:
        store.set_awaiting_edit(chat_id, None)
        telegram_api.send_message(chat_id, "That request has expired, there's nothing to edit anymore.")
        return

    revision_count = store.increment_revision_count(job_id)
    store.set_awaiting_edit(chat_id, None)

    if revision_count > config.MAX_REVISION_COUNT:
        step_functions.send_task_failure(
            pending["task_token"],
            "MaxRevisionsExceeded",
            f"Exceeded {config.MAX_REVISION_COUNT} edit rounds",
        )
        store.delete_pending_approval(job_id)
        telegram_api.send_message(
            chat_id,
            f"That's {config.MAX_REVISION_COUNT} revisions on this one, denying it so it doesn't stall the queue.",
        )
        return

    step_functions.send_task_success(
        pending["task_token"], {"decision": "edit", "edit_feedback": feedback_text}
    )
    telegram_api.send_message(chat_id, "Got it, drafting a revision now.")


def _handle_ad_hoc_question(chat_id: int, text: str) -> None:
    placeholder = telegram_api.send_message(chat_id, _RESEARCHING_TEXT)
    _sqs.send_message(
        QueueUrl=config.QA_QUEUE_URL,
        MessageBody=json.dumps(
            {
                "chat_id": chat_id,
                "question_text": text,
                "placeholder_message_id": placeholder["message_id"],
            }
        ),
    )
