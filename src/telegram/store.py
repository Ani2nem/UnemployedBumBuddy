"""DynamoDB access helpers for PendingApprovals and ConversationState.

Both tables are provisioned by feat/infra; this module only knows the item
shapes this workstream reads and writes:

    PendingApprovals[job_id] = {
        job_id, task_token, telegram_chat_id, telegram_message_id,
        brief_text, draft_text, revision_count, ttl,
    }

    ConversationState[chat_id] = {
        chat_id, last_context_job_id, awaiting_edit_job_id (optional),
    }
"""

from __future__ import annotations

import time
from typing import Any

import boto3

from src.shared.tables import CONVERSATION_STATE_TABLE, PENDING_APPROVALS_TABLE
from src.telegram import config

_dynamodb = boto3.resource("dynamodb")
_pending_approvals = _dynamodb.Table(PENDING_APPROVALS_TABLE)
_conversation_state = _dynamodb.Table(CONVERSATION_STATE_TABLE)


def put_pending_approval(
    job_id: str,
    task_token: str,
    telegram_chat_id: int,
    telegram_message_id: int,
    brief_text: str,
    draft_text: str,
) -> None:
    _pending_approvals.put_item(
        Item={
            "job_id": job_id,
            "task_token": task_token,
            "telegram_chat_id": telegram_chat_id,
            "telegram_message_id": telegram_message_id,
            "brief_text": brief_text,
            "draft_text": draft_text,
            "revision_count": 0,
            "ttl": int(time.time()) + config.PENDING_APPROVAL_TTL_SECONDS,
        }
    )


def get_pending_approval(job_id: str) -> dict[str, Any] | None:
    response = _pending_approvals.get_item(Key={"job_id": job_id})
    return response.get("Item")


def delete_pending_approval(job_id: str) -> None:
    _pending_approvals.delete_item(Key={"job_id": job_id})


def increment_revision_count(job_id: str) -> int:
    response = _pending_approvals.update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET revision_count = revision_count + :one",
        ExpressionAttributeValues={":one": 1},
        ReturnValues="UPDATED_NEW",
    )
    return int(response["Attributes"]["revision_count"])


def get_conversation_state(chat_id: int | str) -> dict[str, Any]:
    response = _conversation_state.get_item(Key={"chat_id": str(chat_id)})
    return response.get("Item") or {"chat_id": str(chat_id)}


def set_last_context_job(chat_id: int | str, job_id: str) -> None:
    _conversation_state.update_item(
        Key={"chat_id": str(chat_id)},
        UpdateExpression="SET last_context_job_id = :job_id",
        ExpressionAttributeValues={":job_id": job_id},
    )


def set_awaiting_edit(chat_id: int | str, job_id: str | None) -> None:
    if job_id is None:
        _conversation_state.update_item(
            Key={"chat_id": str(chat_id)},
            UpdateExpression="REMOVE awaiting_edit_job_id",
        )
    else:
        _conversation_state.update_item(
            Key={"chat_id": str(chat_id)},
            UpdateExpression="SET awaiting_edit_job_id = :job_id",
            ExpressionAttributeValues={":job_id": job_id},
        )


def clear_awaiting_edit_if_matches(chat_id: int | str, job_id: str) -> None:
    """Clear the awaiting-edit flag only if it still points at this job.

    Guards against an Edit tap on one job clobbering an in-flight edit wait
    on a different job sent to the same chat (multiple Map branches can send
    briefs to the same chat close together).
    """
    state = get_conversation_state(chat_id)
    if state.get("awaiting_edit_job_id") == job_id:
        set_awaiting_edit(chat_id, None)
