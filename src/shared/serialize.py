"""JobPosting <-> plain-dict conversion for Lambda/Step Functions I/O.

`JobPosting` is a frozen shared contract (dataclass, not JSON-native -
`posted_at` is a `datetime`), so every Lambda that crosses a Step Functions
state boundary needs to convert it. Kept here rather than on the dataclass
itself since `shared/contracts.py` is treated as the frozen contract module.
Used by the adapters (serializing out) and every pipeline stage handler
(deserializing in, and re-serializing out again after dedup/prefilter).
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Any

from shared.contracts import JobPosting


def job_posting_to_dict(posting: JobPosting) -> dict[str, Any]:
    """`raw_metadata` is deliberately dropped here, not just left as-is.

    It's write-only - no pipeline/telegram code reads it - but adapters
    populate it with the entire raw scraped/API payload per posting
    (confirmed against a live Amazon.jobs response exceeding Lambda's 6MB
    invoke limit, and a single Google Careers posting alone exceeding Step
    Functions' separate 256KB state-transition limit). Every posting crosses
    several state-machine boundaries between here and NotifyTelegram, so
    carrying dead weight through all of them doesn't scale past a handful of
    results. Adapters can still use `raw_metadata` internally before this
    conversion; it just never leaves the adapter Lambda.
    """
    data = dataclasses.asdict(posting)
    data["posted_at"] = posting.posted_at.isoformat() if posting.posted_at else None
    data["job_key"] = posting.job_key
    data["raw_metadata"] = {}
    return data


def job_posting_from_dict(data: dict[str, Any]) -> JobPosting:
    posted_at_raw = data.get("posted_at")
    field_names = {f.name for f in dataclasses.fields(JobPosting)}
    return JobPosting(
        **{
            **{k: v for k, v in data.items() if k in field_names},
            "posted_at": datetime.fromisoformat(posted_at_raw) if posted_at_raw else None,
        }
    )
