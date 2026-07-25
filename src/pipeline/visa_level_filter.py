"""Cheap-rules-first visa sponsorship + experience-level filter.

Pipeline (see docs/ARCHITECTURE.md "Visa + experience-level filtering"):

1. Location - hard exclude if the posting isn't in the candidate's target
   countries.
2. Visa keyword scan - an explicit JD statement (either direction) resolves
   sponsorship immediately, no DOL lookup needed.
3. If the JD is silent, cross-reference `SponsorHistory` (DOL LCA
   disclosures) for that employer in the last `LCA_LOOKBACK_YEARS`. A
   confident match passes; no filings at all is a hard exclude; a fuzzy
   match is genuinely ambiguous and deferred to the LLM stage.
4. Experience-level band - asymmetric, allow up to
   `EXPERIENCE_LEVELS_ABOVE_ALLOWED` levels above the candidate's current
   level, hard-exclude anything below or beyond that ceiling. If the
   posting's level can't be classified from its title or a matched LCA
   filing's `pw_wage_level`, it's deferred to the LLM stage too.
5. Only when visa and/or level are genuinely ambiguous after the above: one
   Nova Lite call resolves whichever dimension(s) are still unresolved.

Visa and level are independent gates - a posting must clear both.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from datetime import date
from typing import Any, Literal

from pipeline import embeddings, llm
from pipeline.config import (
    EXPERIENCE_LEVELS_ABOVE_ALLOWED,
    EXPERIENCE_LEVELS_BELOW_ALLOWED,
    LCA_LOOKBACK_YEARS,
    LCA_SALARY_TOLERANCE_PCT,
    LCA_TITLE_SIMILARITY_THRESHOLD,
    VISA_SPONSORSHIP_NEGATIVE_PHRASES,
    VISA_SPONSORSHIP_POSITIVE_PHRASES,
)
from pipeline.dynamo import Key, get_table
from pipeline.employer_normalize import (
    fuzzy_match_employer,
    known_employer_aliases,
    normalize_employer_name,
)
from pipeline.models import CandidateProfile, ExperienceLevel, FilterResult, SponsorFiling
from pipeline.profile import load_candidate_profile
from shared.contracts import JobPosting
from shared.serialize import job_posting_from_dict, job_posting_to_dict
from shared.tables import SPONSOR_HISTORY_TABLE

LcaOutcome = Literal["match", "no_evidence", "ruled_out", "ambiguous"]


# --- Location ---


def check_location(posting: JobPosting, profile: CandidateProfile) -> bool:
    return posting.country_code.upper() in profile.target_country_codes


# --- Visa keyword scan ---


def scan_visa_keywords(description_text: str) -> bool | None:
    """Explicit sponsorship signal from the JD text, or None if silent.

    Negative phrases are checked first: a hard "we do not sponsor" statement
    should win even in the unlikely case a JD also contains boilerplate
    positive language elsewhere.
    """
    text = description_text.lower()
    if any(phrase in text for phrase in VISA_SPONSORSHIP_NEGATIVE_PHRASES):
        return False
    if any(phrase in text for phrase in VISA_SPONSORSHIP_POSITIVE_PHRASES):
        return True
    return None


# --- Experience level classification ---

# "engineer\s*iv" (etc.) deliberately doesn't require "Software" immediately
# before "Engineer" - real titles vary ("Software Development Engineer II",
# "Data Engineer III", "Site Reliability Engineer II"), and the roman-numeral
# suffix is the actual level signal regardless of what precedes it.
_LEVEL_PATTERNS: tuple[tuple[re.Pattern[str], ExperienceLevel], ...] = (
    (re.compile(r"\b(distinguished|fellow|l9|l8)\b", re.IGNORECASE), ExperienceLevel.DISTINGUISHED),
    (re.compile(r"\b(principal|l7)\b", re.IGNORECASE), ExperienceLevel.PRINCIPAL),
    (
        re.compile(r"\b(staff|l6|sde\s*iv|engineer\s*iv)\b", re.IGNORECASE),
        ExperienceLevel.STAFF,
    ),
    (
        re.compile(r"\b(senior|sr\.?|l5|sde\s*iii|engineer\s*iii)\b", re.IGNORECASE),
        ExperienceLevel.SENIOR,
    ),
    (
        re.compile(r"\b(l4|l3|sde\s*ii|engineer\s*ii)\b", re.IGNORECASE),
        ExperienceLevel.MID,
    ),
    (
        re.compile(
            r"\b(junior|jr\.?|intern|new\s*grad|entry[\s-]?level|associate|l1|l2|"
            r"sde\s*i|engineer\s*i)\b",
            re.IGNORECASE,
        ),
        ExperienceLevel.ENTRY,
    ),
)

_WAGE_LEVEL_TO_EXPERIENCE: dict[str, ExperienceLevel] = {
    "I": ExperienceLevel.ENTRY,
    "II": ExperienceLevel.MID,
    "III": ExperienceLevel.SENIOR,
    "IV": ExperienceLevel.STAFF,
}


def classify_title_level(title: str) -> ExperienceLevel | None:
    """Heuristic seniority classification from a job title's text.

    Checked most-senior-pattern-first so e.g. "Staff" isn't shadowed by a
    coincidental lower-level match. Returns None when the title carries no
    seniority signal at all (common for generic "Software Engineer" titles),
    in which case callers should fall back to a matched LCA filing's
    `pw_wage_level` or, failing that, the LLM stage.
    """
    for pattern, level in _LEVEL_PATTERNS:
        if pattern.search(title):
            return level
    return None


def wage_level_to_experience(pw_wage_level: str | None) -> ExperienceLevel | None:
    if pw_wage_level is None:
        return None
    return _WAGE_LEVEL_TO_EXPERIENCE.get(pw_wage_level.strip().upper())


def level_in_band(posting_level: ExperienceLevel, profile_level: ExperienceLevel) -> bool:
    delta = int(posting_level) - int(profile_level)
    return -EXPERIENCE_LEVELS_BELOW_ALLOWED <= delta <= EXPERIENCE_LEVELS_ABOVE_ALLOWED


# --- Salary parsing (posting side; DOL wage_from/wage_to are already numeric) ---

_SALARY_RANGE_PATTERN = re.compile(
    r"\$?\s*([\d,]+(?:\.\d+)?)\s*(k)?\s*(?:-|to|–|—)\s*\$?\s*([\d,]+(?:\.\d+)?)\s*(k)?",
    re.IGNORECASE,
)


def parse_salary_range(salary_text: str | None) -> tuple[float, float] | None:
    """Best-effort $lo-$hi extraction from free-text salary strings.

    Heuristic, not exhaustive - good enough for the cheap-rules stage. A
    posting with unparseable salary text is treated the same as no salary
    listed (the LCA salary check is skipped, title/level still applies).
    """
    if not salary_text:
        return None
    match = _SALARY_RANGE_PATTERN.search(salary_text)
    if not match:
        return None
    lo_str, lo_k, hi_str, hi_k = match.groups()
    lo = float(lo_str.replace(",", ""))
    hi = float(hi_str.replace(",", ""))
    if lo_k:
        lo *= 1000
    if hi_k:
        hi *= 1000
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def _salary_ranges_overlap(
    posting_range: tuple[float, float],
    filing_range: tuple[float, float],
    tolerance_pct: float = LCA_SALARY_TOLERANCE_PCT,
) -> bool:
    p_lo, p_hi = posting_range
    f_lo, f_hi = filing_range
    span = f_hi - f_lo
    widen = span * tolerance_pct if span > 0 else f_hi * tolerance_pct
    f_lo -= widen
    f_hi += widen
    return p_lo <= f_hi and f_lo <= p_hi


# --- DOL LCA / SponsorHistory matching ---


def fetch_sponsor_filings(
    company: str,
    *,
    as_of: date | None = None,
    employer_normalized_index: Sequence[str] | None = None,
) -> list[SponsorFiling]:
    """Pull `SponsorHistory` filings for `company` from the last
    `LCA_LOOKBACK_YEARS`, across the posting's own normalized name and any
    known alias filer names for that brand.

    `employer_normalized_index` is an optional pre-fetched list of distinct
    `employer_normalized` values already present in SponsorHistory, used as
    the candidate pool for a fuzzy-match fallback when the employer isn't in
    the curated alias table. Omit it (the default) to skip fuzzy fallback
    entirely and rely on exact/alias matches only - a live full-table scan
    per posting would defeat the pipeline's cost-consciousness goal. The DOL
    LCA ETL script is the natural place to produce this index as a cheap
    side artifact (see dol_lca_etl.py), not something computed per request.
    """
    as_of = as_of or date.today()
    cutoff = as_of.replace(year=as_of.year - LCA_LOOKBACK_YEARS)
    normalized = normalize_employer_name(company)

    candidate_keys = {normalized, *known_employer_aliases(normalized)}
    if len(candidate_keys) == 1 and employer_normalized_index:
        fuzzy = fuzzy_match_employer(normalized, list(employer_normalized_index))
        if fuzzy:
            candidate_keys.add(fuzzy)

    table = get_table(SPONSOR_HISTORY_TABLE)
    filings: list[SponsorFiling] = []
    for key in candidate_keys:
        response = table.query(
            KeyConditionExpression=(
                Key("employer_normalized").eq(key)
                & Key("decision_date_case_id").gte(cutoff.isoformat())
            ),
        )
        filings.extend(_item_to_filing(item, key) for item in response.get("Items", []))
    return filings


def _item_to_filing(item: dict, employer_normalized: str) -> SponsorFiling:
    decision_date_str, _, case_id = item["decision_date_case_id"].partition("#")
    return SponsorFiling(
        employer_normalized=employer_normalized,
        decision_date=date.fromisoformat(decision_date_str),
        case_id=case_id,
        job_title=item.get("job_title", ""),
        soc_title=item.get("soc_title", ""),
        wage_from=float(item["wage_from"]) if item.get("wage_from") is not None else None,
        wage_to=float(item["wage_to"]) if item.get("wage_to") is not None else None,
        pw_wage_level=item.get("pw_wage_level"),
    )


def _embedding_title_similarity(title_a: str, title_b: str) -> float:
    return embeddings.cosine_similarity(embeddings.embed_text(title_a), embeddings.embed_text(title_b))


def evaluate_lca_match(
    posting: JobPosting,
    filings: list[SponsorFiling],
    *,
    title_similarity_fn: Callable[[str, str], float] | None = _embedding_title_similarity,
) -> tuple[LcaOutcome, SponsorFiling | None]:
    """Decide whether `filings` provide evidence this employer sponsors visas
    for a role like `posting`.

    Per filing: a salary range on the posting that clearly doesn't overlap
    the filing's wage range (with tolerance) rules that filing out entirely.
    Otherwise, title/seniority is compared via `pw_wage_level` first (free)
    and `title_similarity_fn` second (a Titan embedding call) when the wage
    level is missing or the posting's title carries no level signal.

    Returns ("match", filing) on the first confident match, ("ambiguous",
    filing) if at least one filing couldn't be confidently ruled in or out,
    ("ruled_out", None) if every filing was confidently inconsistent, or
    ("no_evidence", None) if there were no filings at all. "ruled_out" and
    "no_evidence" are both hard excludes; only "ambiguous" goes to the LLM.
    """
    if not filings:
        return "no_evidence", None

    posting_salary_range = parse_salary_range(posting.salary_text)
    posting_level = classify_title_level(posting.title)

    found_ambiguous: SponsorFiling | None = None
    for filing in filings:
        if (
            posting_salary_range is not None
            and filing.wage_from is not None
            and filing.wage_to is not None
            and not _salary_ranges_overlap(posting_salary_range, (filing.wage_from, filing.wage_to))
        ):
            continue  # confidently ruled out by salary

        filing_level = wage_level_to_experience(filing.pw_wage_level)
        if posting_level is not None and filing_level is not None:
            if posting_level == filing_level:
                return "match", filing
            continue  # confidently ruled out by level mismatch

        if title_similarity_fn is not None:
            score = title_similarity_fn(posting.title, filing.job_title or filing.soc_title)
            if score >= LCA_TITLE_SIMILARITY_THRESHOLD:
                return "match", filing

        found_ambiguous = found_ambiguous or filing

    if found_ambiguous is not None:
        return "ambiguous", found_ambiguous
    return "ruled_out", None


# --- LLM fallback for genuinely ambiguous cases ---

_LLM_SYSTEM_PROMPT = (
    "You are a filtering assistant for a job-application pipeline. Given a job "
    "posting and a candidate's current level, decide two things and respond with "
    'ONLY a JSON object of the form {"visa_likely": true|false, "level_match": '
    'true|false, "rationale": "one sentence"}. '
    "visa_likely: whether this employer/role is plausibly open to visa sponsorship "
    "given the evidence provided. level_match: whether the role's seniority is a "
    "reasonable fit for the candidate - be ambitious, treat roles up to two levels "
    "above the candidate's current level as a match, and exclude only roles clearly "
    "below the candidate's level or dramatically more senior."
)


def _build_llm_user_prompt(
    posting: JobPosting, profile: CandidateProfile, lca_context: str
) -> str:
    return (
        f"Job title: {posting.title}\n"
        f"Company: {posting.company}\n"
        f"Location: {posting.location} ({posting.country_code})\n"
        f"Candidate current level: {profile.current_level.name}\n"
        f"Sponsorship evidence so far: {lca_context}\n"
        f"Job description (may be truncated):\n{posting.description_text[:4000]}"
    )


# --- Orchestration ---


def filter_posting(
    posting: JobPosting,
    profile: CandidateProfile,
    *,
    as_of: date | None = None,
    employer_normalized_index: Sequence[str] | None = None,
    title_similarity_fn: Callable[[str, str], float] | None = _embedding_title_similarity,
    llm_invoke: Callable[[str, str], dict] = llm.invoke_json,
) -> FilterResult:
    if not check_location(posting, profile):
        return FilterResult(
            passed=False,
            stage="location",
            reason=f"country_code {posting.country_code!r} not in {profile.target_country_codes}",
        )

    keyword_signal = scan_visa_keywords(posting.description_text)
    lca_filing: SponsorFiling | None = None
    lca_context = ""

    if keyword_signal is False:
        return FilterResult(
            passed=False,
            stage="visa_keyword_exclude",
            reason="JD explicitly states no visa sponsorship",
            visa_likely=False,
        )
    elif keyword_signal is True:
        visa_likely: bool | None = True
        lca_context = "JD explicitly states sponsorship is available."
    else:
        filings = fetch_sponsor_filings(
            posting.company, as_of=as_of, employer_normalized_index=employer_normalized_index
        )
        lca_outcome, lca_filing = evaluate_lca_match(
            posting, filings, title_similarity_fn=title_similarity_fn
        )
        if lca_outcome == "match":
            visa_likely = True
            lca_context = f"Matched DOL LCA filing {lca_filing.case_id} ({lca_filing.decision_date})."
        elif lca_outcome in ("no_evidence", "ruled_out"):
            return FilterResult(
                passed=False,
                stage=f"visa_lca_{lca_outcome}",
                reason="No DOL LCA filing evidence of sponsorship for this employer in the lookback window",
                visa_likely=False,
            )
        else:  # ambiguous
            visa_likely = None
            lca_context = (
                f"JD silent; found a fuzzy/inconclusive DOL LCA filing "
                f"({lca_filing.job_title if lca_filing else 'unknown title'})."
            )

    posting_level = classify_title_level(posting.title)
    filing_level = wage_level_to_experience(lca_filing.pw_wage_level) if lca_filing else None
    effective_level = posting_level if posting_level is not None else filing_level

    if effective_level is not None:
        level_match: bool | None = level_in_band(effective_level, profile.current_level)
        if level_match is False:
            return FilterResult(
                passed=False,
                stage="experience_level",
                reason=(
                    f"posting level {effective_level.name} outside allowed band "
                    f"for profile level {profile.current_level.name}"
                ),
                visa_likely=visa_likely,
                level_match=False,
            )
    else:
        level_match = None

    if visa_likely is None or level_match is None:
        llm_result = llm_invoke(_LLM_SYSTEM_PROMPT, _build_llm_user_prompt(posting, profile, lca_context))
        resolved_visa = llm_result.get("visa_likely") if visa_likely is None else visa_likely
        resolved_level = llm_result.get("level_match") if level_match is None else level_match
        passed = bool(resolved_visa) and bool(resolved_level)
        return FilterResult(
            passed=passed,
            stage="llm_ambiguous",
            reason=llm_result.get("rationale", "resolved by Nova Lite fallback"),
            visa_likely=bool(resolved_visa),
            level_match=bool(resolved_level),
        )

    return FilterResult(
        passed=True,
        stage="passed",
        reason="passed all rule-based filters",
        visa_likely=visa_likely,
        level_match=level_match,
    )


# --- Lambda entrypoint (Step Functions "RuleBasedPrefilter") ---


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Event: `{"candidates": [JobPosting dict, ...]}` (Dedup's output).

    Returns only the postings that passed both the visa and experience-level
    gates. Doesn't record `FilterResult` reasons for excluded postings
    anywhere yet - `RecordJobFailed`/`UpdateJobStatus` only fires for
    downstream pipeline errors, not rule-based exclusions, so a filtered-out
    posting is currently silent rather than logged as `FILTERED_OUT`.
    """
    profile = load_candidate_profile()
    candidates = [job_posting_from_dict(raw) for raw in event.get("candidates") or []]
    passed = [posting for posting in candidates if filter_posting(posting, profile).passed]
    return {"candidates": [job_posting_to_dict(p) for p in passed]}
