"""SQS consumer that answers ad hoc Telegram questions asynchronously.

Triggered by the QA queue that webhook.py pushes to. Runs the Serper + Nova
Lite research call that must never happen inline inside the webhook (API
Gateway's 29s timeout), resolves which job the question is about via
ConversationState[chat_id].last_context_job_id, then edits the
"researching..." placeholder message with the answer.

Assumes the event source mapping has ReportBatchItemFailures enabled (owned
by feat/infra) so a failure on one message doesn't requeue the whole batch.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.telegram import research, store, telegram_api

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    failures = []
    for record in event.get("Records", []):
        try:
            _handle_record(record)
        except Exception:
            logger.exception("Failed to process QA record %s", record.get("messageId"))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


def _handle_record(record: dict[str, Any]) -> None:
    body = json.loads(record["body"])
    chat_id = body["chat_id"]
    question_text = body["question_text"]
    placeholder_message_id = body.get("placeholder_message_id")

    job_context = _resolve_job_context(chat_id)

    try:
        search_results = research.search_web(question_text)
        answer = research.answer_question(question_text, job_context, search_results)
    except Exception:
        logger.exception("Failed to research ad hoc question for chat %s", chat_id)
        answer = "Sorry, I couldn't research that just now. Try asking again in a bit."

    _post_answer(chat_id, placeholder_message_id, answer)


def _resolve_job_context(chat_id: int) -> str:
    conversation_state = store.get_conversation_state(chat_id)
    job_id = conversation_state.get("last_context_job_id")
    if not job_id:
        return ""
    pending = store.get_pending_approval(job_id)
    if not pending:
        return ""
    return f"{pending['brief_text']}\n\n{pending['draft_text']}"


def _post_answer(chat_id: int, placeholder_message_id: int | None, answer: str) -> None:
    if placeholder_message_id is not None:
        try:
            telegram_api.edit_message_text(chat_id, placeholder_message_id, answer)
            return
        except Exception:
            logger.exception("Failed to edit placeholder message %s", placeholder_message_id)
    telegram_api.send_message(chat_id, answer)
