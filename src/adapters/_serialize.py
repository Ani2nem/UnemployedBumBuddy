"""JobPosting -> plain-dict serialization for Lambda/Step Functions handler output.

`JobPosting` is a frozen shared contract (dataclass, not JSON-native - `posted_at` is
a `datetime`), so each adapter's Lambda entry point needs to convert it before
returning to Step Functions. Kept here rather than on the dataclass itself since
`shared/contracts.py` is read-only from this branch.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from shared.contracts import JobPosting


def job_posting_to_dict(posting: JobPosting) -> dict[str, Any]:
    data = dataclasses.asdict(posting)
    data["posted_at"] = posting.posted_at.isoformat() if posting.posted_at else None
    data["job_key"] = posting.job_key
    return data
