"""Pipeline-internal data models.

These are distinct from `src/shared/contracts.py` (frozen, cross-workstream)
and `src/shared/tables.py` (DynamoDB key names). Everything here is consumed
only within `src/pipeline/` - the subset of `ApplicantProfile`/`Projects`/
`StyleExamples`/`SponsorHistory` item shape that the filtering, research,
matching, and drafting logic actually needs, not the full table schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import IntEnum


class ExperienceLevel(IntEnum):
    """Ordinal seniority scale used to compare a posting against the profile.

    Company-specific ladders (Amazon L4-L8, Google L3-L8, "Senior"/"Staff"/
    "Principal" titles, DOL pw_wage_level I-IV) all map onto this scale so
    filtering logic never has to reason about ladder-specific strings.
    """

    ENTRY = 0  # new grad / L3 / SDE I / Level I
    MID = 1  # L4 / SDE II / Level II
    SENIOR = 2  # L5 / Senior / Level III
    STAFF = 3  # L6 / Staff / Level IV
    PRINCIPAL = 4  # L7 / Principal
    DISTINGUISHED = 5  # L8+ / Distinguished / Fellow


@dataclass
class CandidateProfile:
    """The slice of `ApplicantProfile` the pipeline filters against."""

    profile_id: str
    current_level: ExperienceLevel
    target_country_codes: tuple[str, ...] = ("US",)


@dataclass
class SponsorFiling:
    """One row from `SponsorHistory` (a single DOL LCA disclosure record)."""

    employer_normalized: str
    decision_date: date
    case_id: str
    job_title: str
    soc_title: str
    wage_from: float | None
    wage_to: float | None
    pw_wage_level: str | None  # DOL wage level I-IV, or None if unavailable


@dataclass
class FilterResult:
    passed: bool
    stage: str
    reason: str
    visa_likely: bool | None = None
    level_match: bool | None = None


@dataclass
class ResearchResult:
    company_normalized: str
    brief_text: str
    tone_guidance: str
    sources: list[str] = field(default_factory=list)
    cached: bool = False


@dataclass
class ProjectEmbedding:
    project_id: str
    title: str
    embedding: list[float]


@dataclass
class ProjectMatch:
    project_id: str
    title: str
    similarity: float


@dataclass
class StyleExample:
    example_id: str
    scenario_tag: str
    text: str
    embedding: list[float] | None = None


@dataclass
class DraftResult:
    draft_text: str
    projects_referenced: list[str]
    confidence_notes: str
