"""Dedup against `SeenJobs` (DynamoDB), keyed by `JobPosting.job_key`.

Uses a conditional put rather than "check then write" so that two concurrent
scans (e.g. an hourly run overlapping a manual re-trigger) can't both decide
the same posting is new - only the put that actually inserts the row wins.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError

from pipeline.dynamo import get_table
from shared.contracts import JobPosting
from shared.serialize import job_posting_from_dict, job_posting_to_dict
from shared.tables import SEEN_JOBS_STATUS_VALUES, SEEN_JOBS_TABLE


def try_claim_new(posting: JobPosting, *, now: datetime | None = None) -> bool:
    """Atomically claim `posting.job_key` as newly seen.

    Returns True if this call inserted the record (the posting is new to the
    pipeline). Returns False if the job_key already exists - another scan
    claimed it first, or it was already processed in a prior cycle.
    """
    ts = (now or datetime.now(UTC)).isoformat()
    try:
        get_table(SEEN_JOBS_TABLE).put_item(
            Item={
                "job_key": posting.job_key,
                "source": posting.source,
                "external_id": posting.external_id,
                "company": posting.company,
                "title": posting.title,
                "status": "NEW",
                "first_seen_at": ts,
                "last_seen_at": ts,
            },
            ConditionExpression="attribute_not_exists(job_key)",
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def filter_new(postings: list[JobPosting]) -> list[JobPosting]:
    """Claim each posting in `SeenJobs`; return only the ones that were new.

    Intended as the pipeline's first stage: everything downstream (filter,
    research, draft) only ever sees postings this call let through.
    """
    return [posting for posting in postings if try_claim_new(posting)]


def is_seen(job_key: str) -> bool:
    response = get_table(SEEN_JOBS_TABLE).get_item(
        Key={"job_key": job_key}, ProjectionExpression="job_key"
    )
    return "Item" in response


def get_seen_job(job_key: str) -> dict | None:
    response = get_table(SEEN_JOBS_TABLE).get_item(Key={"job_key": job_key})
    return response.get("Item")


def update_status(job_key: str, status: str, **extra_attrs: object) -> None:
    """Move a `SeenJobs` row to a new status, bumping `last_seen_at`.

    `extra_attrs` are set alongside the status update (e.g. a filter stage
    recording its `reason`) - attribute names are used verbatim as DynamoDB
    update-expression paths, so callers must pass safe, non-reserved names.
    """
    if status not in SEEN_JOBS_STATUS_VALUES:
        raise ValueError(f"Unknown SeenJobs status: {status!r}")

    now = datetime.now(UTC).isoformat()
    set_clauses = ["#status = :status", "last_seen_at = :now"]
    expr_attr_names = {"#status": "status"}
    expr_attr_values: dict[str, object] = {":status": status, ":now": now}

    for key, value in extra_attrs.items():
        placeholder = f":{key}"
        set_clauses.append(f"{key} = {placeholder}")
        expr_attr_values[placeholder] = value

    get_table(SEEN_JOBS_TABLE).update_item(
        Key={"job_key": job_key},
        UpdateExpression="SET " + ", ".join(set_clauses),
        ExpressionAttributeNames=expr_attr_names,
        ExpressionAttributeValues=expr_attr_values,
    )


# --- Lambda entrypoint (Step Functions "DedupAgainstSeenJobs") ---


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Event: `{"branch_postings": [[JobPosting dict, ...], ...]}` - one list
    per adapter branch, per the ASL's `ScanSources` `ResultSelector`.
    """
    branch_postings = event.get("branch_postings") or []
    postings = [
        job_posting_from_dict(raw) for branch in branch_postings for raw in (branch or [])
    ]
    new_postings = filter_new(postings)
    return {"candidates": [job_posting_to_dict(p) for p in new_postings]}
