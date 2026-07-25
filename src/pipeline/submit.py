"""Submission: MVP is manual-submit only.

Per docs/ARCHITECTURE.md "Submission pipeline", ATS auto-submission
(Greenhouse/Lever/Ashby) is Phase 2, gated on per-employer capability
probing that hasn't been built. This Lambda's only job right now is to
record that a draft was approved and hand back what's needed for the human
to submit it themselves - it never calls an ATS API or sends anything via
SES today, even when `job.ats_platform` is set.
"""

from __future__ import annotations

from typing import Any

from shared.contracts import JobPosting
from shared.serialize import job_posting_from_dict


def submit_application(job: JobPosting, draft: dict[str, Any]) -> dict[str, Any]:
    return {
        "method": "manual",
        "status": "held_for_manual_submission",
        "apply_url": job.url,
        "ats_platform": job.ats_platform,
        "draft_text": draft.get("draft_text", ""),
    }


# --- Lambda entrypoint (Step Functions "SubmitApplication") ---


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Event: `{"job": JobPosting dict, "draft": {...}}`."""
    job = job_posting_from_dict(event["job"])
    draft = event.get("draft") or {}
    return submit_application(job, draft)
