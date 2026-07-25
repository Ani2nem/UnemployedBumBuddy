"""Rate-limit guard: per-company caps + a platform-wide hourly cap for
Amazon/Google specifically, per docs/ARCHITECTURE.md "Anti-auto-rejection
guardrails".

`ApplicationEvents` is keyed `{pk: company_platform, sk: applied_at}` (frozen
in `shared/tables.py`) but this guard needs two independent access patterns -
"submissions to this company recently" and "submissions to this platform
recently" - and there's no GSI on the table for the second one. Rather than
add one, `update_job_status.py` writes *two* immutable rows per submission,
one under `company#{normalized}` and one under `platform#{source}`, so both
patterns are a plain `Query` against the existing key, a common single-table
DynamoDB pattern.

`RateLimitPolicy` rows are hand-edited config (per the architecture doc),
keyed by `scope_key` matching those same prefixes (e.g. `"platform:amazon"`,
`"company:openai"`, or `"company:default"` as the fallback every company
without its own override uses). A scope with no policy row at all is treated
as uncapped (fail-open) - this guard is a safety net, not something that
should block a fresh deploy before you've hand-seeded any policy data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pipeline.dynamo import Key, get_table
from pipeline.employer_normalize import normalize_employer_name
from shared.contracts import JobPosting
from shared.serialize import job_posting_from_dict
from shared.tables import APPLICATION_EVENTS_TABLE, RATE_LIMIT_POLICY_TABLE

Decision = str  # "proceed" | "cooldown"


def company_scope_key(company: str) -> str:
    return f"company#{normalize_employer_name(company)}"


def platform_scope_key(source: str) -> str:
    return f"platform#{source}"


def _get_policy(scope_key: str) -> dict | None:
    return get_table(RATE_LIMIT_POLICY_TABLE).get_item(Key={"scope_key": scope_key}).get("Item")


def _count_recent(scope_key: str, window_hours: float, *, now: datetime) -> int:
    cutoff = (now - timedelta(hours=window_hours)).isoformat()
    table = get_table(APPLICATION_EVENTS_TABLE)
    count = 0
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("company_platform").eq(scope_key) & Key("applied_at").gte(cutoff),
        "Select": "COUNT",
    }
    while True:
        response = table.query(**kwargs)
        count += response["Count"]
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return count
        kwargs["ExclusiveStartKey"] = last_key


def check_rate_limit(job: JobPosting, *, now: datetime | None = None) -> Decision:
    now = now or datetime.now(UTC)

    platform_policy = _get_policy(f"platform:{job.source}")
    if platform_policy is not None:
        count = _count_recent(
            platform_scope_key(job.source), float(platform_policy["window_hours"]), now=now
        )
        if count >= int(platform_policy["max_count"]):
            return "cooldown"

    normalized_company = normalize_employer_name(job.company)
    company_policy = _get_policy(f"company:{normalized_company}") or _get_policy("company:default")
    if company_policy is not None:
        count = _count_recent(
            company_scope_key(job.company), float(company_policy["window_hours"]), now=now
        )
        if count >= int(company_policy["max_count"]):
            return "cooldown"

    return "proceed"


# --- Lambda entrypoint (Step Functions "RateLimitGuard") ---


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Event: `{"job": JobPosting dict}`. Returns `{"decision": "proceed" |
    "cooldown"}` - the ASL's `IsRateLimited` Choice checks this exact field.
    """
    job = job_posting_from_dict(event["job"])
    return {"decision": check_rate_limit(job)}
