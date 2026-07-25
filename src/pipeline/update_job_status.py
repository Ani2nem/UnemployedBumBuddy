"""Terminal status recorder - every branch of the ASL's per-candidate loop
ends here (`RecordSubmitted`, `RecordDenied`, `MarkQueuedCooldown`,
`RecordMaxEditsReached`, `RecordApprovalTimeout`, `RecordJobFailed`).

Always updates `SeenJobs`. On `SUBMITTED`, additionally writes the two
`ApplicationEvents` rows `rate_limit_guard.py` depends on for its rolling-
window counts - this is the only place those rows get written, since
`submit.py` itself doesn't touch DynamoDB.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pipeline import dedup, rate_limit_guard
from pipeline.dynamo import get_table
from shared.contracts import JobPosting
from shared.serialize import job_posting_from_dict
from shared.tables import APPLICATION_EVENTS_TABLE

_EXTRA_ATTR_KEYS = ("reason", "error", "submission")


def record_application_event(job: JobPosting, *, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    applied_at_iso = now.isoformat()
    table = get_table(APPLICATION_EVENTS_TABLE)
    base_item = {
        "applied_at": f"{applied_at_iso}#{job.job_key}",
        "job_key": job.job_key,
        "company": job.company,
        "source": job.source,
        "recorded_at": applied_at_iso,
    }
    table.put_item(
        Item={**base_item, "company_platform": rate_limit_guard.company_scope_key(job.company)}
    )
    table.put_item(
        Item={**base_item, "company_platform": rate_limit_guard.platform_scope_key(job.source)}
    )


# --- Lambda entrypoint (Step Functions "UpdateJobStatus", multiple call sites) ---


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Event: `{"job": JobPosting dict, "status": "...", [reason], [error],
    [submission]}` - which optional fields are present depends on which ASL
    state called this (see the module docstring).
    """
    job = job_posting_from_dict(event["job"])
    status = event["status"]
    extra_attrs = {k: event[k] for k in _EXTRA_ATTR_KEYS if k in event}

    dedup.update_status(job.job_key, status, **extra_attrs)
    if status == "SUBMITTED":
        record_application_event(job)

    return {"job_key": job.job_key, "status": status}
