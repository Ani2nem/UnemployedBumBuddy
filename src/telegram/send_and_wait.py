"""Lambda invoked by Step Functions' `lambda:invoke.waitForTaskToken` integration.

Owned here because it's the piece of the Map-state loop that talks to
Telegram; the state machine definition itself belongs to `infra/`.

Actual event shape, built by the ASL's `NotifyTelegram` state (see
`infra/step_functions/job_scan_orchestrator.asl.json`), using `$$.Task.Token`
for the token:

    {
        "task_token": "<$$.Task.Token>",
        "job": {...JobPosting dict, incl. "job_key"...},
        "research": {"brief_text": "...", "tone_guidance": "...", ...},
        "project_match": {"matches": [...]},
        "draft": {"draft_text": "...", ...},
        "edit_count": 0
    }

Single-user tool: the chat to notify is `config.TELEGRAM_CHAT_ID`, not
something passed per-job - the ASL has no per-job chat id to give us.

Sends the brief + draft with an Approve/Deny/Edit inline keyboard, persists
the task token into PendingApprovals, and returns immediately - the human
response resumes the execution later via webhook.py, not this Lambda.
"""

from __future__ import annotations

from typing import Any

from telegram import config, store, telegram_api


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    job_id = event["job"]["job_key"]
    task_token = event["task_token"]
    chat_id = config.TELEGRAM_CHAT_ID
    brief_text = event.get("research", {}).get("brief_text", "")
    draft = event.get("draft", {})
    draft_text = draft.get("draft_text", "")
    confidence_notes = draft.get("confidence_notes", "")

    edit_count = event.get("edit_count", 0)
    prefix = f"✏️ Revision {edit_count}\n\n" if edit_count else ""
    # confidence_notes exists specifically to inform the approve/deny call
    # (see draft.py's system prompt) - dropping it here would silently
    # discard the one piece of the draft aimed at the human reviewer rather
    # than at the recipient of the message itself.
    confidence_block = f"\n\n⚠️ {confidence_notes}" if confidence_notes else ""
    message_text = f"{prefix}{brief_text}\n\n{draft_text}{confidence_block}"

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
