"""Lambda invoked by Step Functions' `lambda:invoke.waitForTaskToken` integration.

Owned here because it's the piece of the Map-state loop that talks to
Telegram; the state machine definition itself belongs to feat/infra.

Expected event (the Step Functions "Payload", built via the ASL's
Parameters block, using `$$.Task.Token` for the token):

    {
        "task_token": "<$$.Task.Token>",
        "job_id": "amazon#12345",
        "telegram_chat_id": 123456789,
        "brief_text": "...",
        "draft_text": "..."
    }

Sends the brief + draft with an Approve/Deny/Edit inline keyboard, persists
the task token into PendingApprovals, and returns immediately - the human
response resumes the execution later via webhook.py, not this Lambda.
"""

from __future__ import annotations

from typing import Any

from src.telegram import store, telegram_api


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    job_id = event["job_id"]
    task_token = event["task_token"]
    chat_id = event["telegram_chat_id"]
    brief_text = event["brief_text"]
    draft_text = event["draft_text"]

    message_text = f"{brief_text}\n\n{draft_text}"
    sent = telegram_api.send_message(
        chat_id, message_text, reply_markup=telegram_api.approval_keyboard(job_id)
    )

    store.put_pending_approval(
        job_id=job_id,
        task_token=task_token,
        telegram_chat_id=chat_id,
        telegram_message_id=sent["message_id"],
        brief_text=brief_text,
        draft_text=draft_text,
    )
    store.set_last_context_job(chat_id, job_id)

    return {"job_id": job_id, "telegram_message_id": sent["message_id"]}
