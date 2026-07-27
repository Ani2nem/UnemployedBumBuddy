"""Loads the single `ApplicantProfile` row into the pipeline's `CandidateProfile`.

Single-user tool - there is exactly one profile, under `DEFAULT_PROFILE_ID`.
Ingestion (writing that row from a resume/LinkedIn export) is a separate,
one-time setup step, not part of the hourly scan loop; this module only
reads it.
"""

from __future__ import annotations

from pipeline.config import DEFAULT_PROFILE_ID
from pipeline.dynamo import get_table
from pipeline.models import CandidateProfile, ExperienceLevel
from shared.tables import APPLICANT_PROFILE_TABLE


class ProfileNotFoundError(RuntimeError):
    def __init__(self, profile_id: str):
        super().__init__(
            f"ApplicantProfile[{profile_id!r}] not found - run profile ingestion before "
            "starting the scan loop."
        )


def load_candidate_profile(profile_id: str = DEFAULT_PROFILE_ID) -> CandidateProfile:
    item = get_table(APPLICANT_PROFILE_TABLE).get_item(Key={"profile_id": profile_id}).get("Item")
    if item is None:
        raise ProfileNotFoundError(profile_id)

    target_country_codes = tuple(item.get("target_country_codes") or ("US",))
    return CandidateProfile(
        profile_id=profile_id,
        current_level=ExperienceLevel[item["current_level"]],
        target_country_codes=target_country_codes,
        background_summary=item.get("background_summary", ""),
    )
