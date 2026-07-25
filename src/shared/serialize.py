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
    data = dataclasses.asdict(posting)
    data["posted_at"] = posting.posted_at.isoformat() if posting.posted_at else None
    data["job_key"] = posting.job_key
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
