"""Employer-name normalization for matching job postings against DOL LCA filings.

DOL disclosure data is filed under the legal entity name, which frequently
differs from the brand name on a job posting (Amazon and Alphabet each file
under many subsidiary LLC names). This module normalizes names to a common
form and maintains a hand-curated alias table for known target companies,
falling back to fuzzy string matching for everyone else - both flagged as
the intended approach in docs/ARCHITECTURE.md.
"""

from __future__ import annotations

import difflib
import re

_LEGAL_SUFFIXES = (
    "incorporated",
    "corporation",
    "company",
    "limited",
    "holdings",
    "holding",
    "llc",
    "inc",
    "corp",
    "ltd",
    "llp",
    "lp",
    "co",
    "plc",
)

_SUFFIX_PATTERN = re.compile(
    r"\b(" + "|".join(_LEGAL_SUFFIXES) + r")\b\.?", re.IGNORECASE
)
_NON_ALNUM_PATTERN = re.compile(r"[^a-z0-9 ]")
_WHITESPACE_PATTERN = re.compile(r"\s+")


def normalize_employer_name(name: str) -> str:
    """Canonicalize an employer name for use as a SponsorHistory lookup key.

    Lowercases, strips punctuation, drops common legal-entity suffixes, and
    collapses whitespace. Applied identically to posting company names and
    DOL filer names so both land on the same key when they refer to the same
    entity.
    """
    lowered = name.lower().strip()
    lowered = lowered.replace("&", "and")
    no_punct = _NON_ALNUM_PATTERN.sub(" ", lowered)
    no_suffix = _SUFFIX_PATTERN.sub(" ", no_punct)
    return _WHITESPACE_PATTERN.sub(" ", no_suffix).strip()


# Brand key -> normalized DOL filer-name variants known to file LCAs for that
# brand. Extend this as new mismatches are discovered; it only needs to cover
# companies actively targeted by the job search, per the architecture doc's
# "known target companies" framing.
KNOWN_EMPLOYER_ALIASES: dict[str, tuple[str, ...]] = {
    "amazon": (
        "amazon com services",
        "amazon com",
        "amazon web services",
        "amazon corporate",
        "amazon data services",
        "amazon dev center",
        "amazon development center",
        "a9 com",
        "audible",
        "twitch interactive",
        "zoox",
        "ring",
        "whole foods market",
    ),
    "google": (
        "google",
        "alphabet",
        "youtube",
        "waymo",
        "verily life sciences",
        "google cloud",
    ),
}

_ALIAS_TO_BRAND: dict[str, str] = {
    normalize_employer_name(alias): brand
    for brand, aliases in KNOWN_EMPLOYER_ALIASES.items()
    for alias in (brand, *aliases)
}


def known_employer_aliases(normalized_name: str) -> tuple[str, ...]:
    """Return every normalized filer-name variant that should be treated as
    the same employer as `normalized_name`, if it matches a known brand.

    Returns an empty tuple when the name isn't in the curated alias table -
    callers should fall back to `fuzzy_match_employer` against whatever
    `employer_normalized` keys actually exist in SponsorHistory.
    """
    brand = _ALIAS_TO_BRAND.get(normalized_name)
    if brand is None:
        return ()
    return tuple(
        normalize_employer_name(alias)
        for alias in (brand, *KNOWN_EMPLOYER_ALIASES[brand])
    )


def fuzzy_match_employer(
    target_normalized: str, candidates: list[str], threshold: float = 0.85
) -> str | None:
    """Best fuzzy match for `target_normalized` among `candidates`, or None.

    Fallback path for employers not in the curated alias table. `candidates`
    should be the set of distinct `employer_normalized` values already
    present in SponsorHistory (cheap to hold in memory - it's one row per
    distinct filer, not per filing).
    """
    if not candidates:
        return None
    matches = difflib.get_close_matches(
        target_normalized, candidates, n=1, cutoff=threshold
    )
    return matches[0] if matches else None
