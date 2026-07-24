"""Shared contracts every workstream codes against.

Treat this module as frozen once parallel work starts across worktrees.
Changes here ripple into every adapter/pipeline/telegram branch, so they
must land on `main` and be rebased into feature branches deliberately,
not patched from a side branch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

AtsPlatform = Literal["greenhouse", "lever", "ashby", "none"]


@dataclass
class JobPosting:
    source: str
    external_id: str
    title: str
    company: str
    location: str
    country_code: str
    remote_flag: bool
    url: str
    description_text: str
    posted_at: datetime | None
    salary_text: str | None
    ats_platform: AtsPlatform
    ats_board_token: str | None
    ats_job_id: str | None
    raw_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def job_key(self) -> str:
        return f"{self.source}#{self.external_id}"


class JobSourceAdapter(Protocol):
    """One implementation per job source (Amazon, Google, Wellfound, Handshake, ...)."""

    source_name: str

    def fetch_new_postings(
        self, since: datetime | None, filters: dict[str, Any]
    ) -> list[JobPosting]:
        ...
