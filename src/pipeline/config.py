"""Tunables for the pipeline workstream: keyword lists, thresholds, model ids.

Kept as plain module constants (not env vars) so filtering behavior is easy to
diff in code review. If these need to be hand-edited at runtime without a
deploy, promote them to a `SourceConfig`-style DynamoDB item later - not
needed yet.
"""

from __future__ import annotations

# --- Bedrock model ids ---
NOVA_LITE_MODEL_ID = "amazon.nova-lite-v1:0"
TITAN_EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"
TITAN_EMBED_DIMENSIONS = 1024

# --- Visa keyword scan ---
# Either list matching anywhere in the JD text wins immediately - no further
# filtering stages run. Keep phrases lowercase; matching is done on lowercased text.
VISA_SPONSORSHIP_POSITIVE_PHRASES = (
    "will sponsor",
    "visa sponsorship available",
    "sponsorship available",
    "h-1b sponsorship",
    "h1b sponsorship",
    "we sponsor",
    "able to sponsor",
    "sponsor visas",
    "sponsor employment visas",
    "sponsor work visas",
    "green card sponsorship",
)

VISA_SPONSORSHIP_NEGATIVE_PHRASES = (
    "must be authorized to work",
    "must be legally authorized to work",
    "without the need for sponsorship",
    "without sponsorship",
    "no sponsorship",
    "not provide sponsorship",
    # Catches "will/does/do/can not sponsor" - all contain this substring.
    "not sponsor",
    "unable to sponsor",
    "u.s. citizens only",
    "us citizens only",
    "must be a us citizen",
    "security clearance",
)

# --- Location filter ---
US_COUNTRY_CODES = ("US", "USA")

# --- DOL LCA matching ---
# Filings older than this are not considered evidence of *current* sponsorship
# willingness (see docs/ARCHITECTURE.md "Visa + experience-level filtering").
LCA_LOOKBACK_YEARS = 2

# Salary match tolerance: a posting's salary range is considered consistent
# with a filing if the ranges overlap after widening the filing by this
# fraction (LCA wage data lags market postings by months to a year).
LCA_SALARY_TOLERANCE_PCT = 0.15

# Title-similarity threshold (cosine, Titan embeddings) above which a filing's
# job_title/soc_title is considered a match for the posting title when
# pw_wage_level alone is ambiguous.
LCA_TITLE_SIMILARITY_THRESHOLD = 0.60

# --- Experience level band ---
# Asymmetric per the "be ambitious" instruction: allow ~+2 levels above the
# profile's current level, hard-exclude anything clearly below or beyond that
# ambitious ceiling (i.e. "dramatically above" == more than this many levels up).
EXPERIENCE_LEVELS_ABOVE_ALLOWED = 2
EXPERIENCE_LEVELS_BELOW_ALLOWED = 0

# --- Company research cache ---
COMPANY_RESEARCH_CACHE_TTL_DAYS = 14

# --- Project match ---
PROJECT_MATCH_SIMILARITY_THRESHOLD = 0.55
PROJECT_MATCH_TOP_K = 3
