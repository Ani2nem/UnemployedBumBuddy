from __future__ import annotations

from pipeline.employer_normalize import (
    fuzzy_match_employer,
    known_employer_aliases,
    normalize_employer_name,
)


def test_normalize_strips_legal_suffixes_and_punctuation():
    assert normalize_employer_name("Amazon.com Services LLC") == "amazon com services"
    assert normalize_employer_name("Acme, Inc.") == "acme"
    assert normalize_employer_name("Beta Corp.") == "beta"


def test_normalize_preserves_descriptive_words_that_look_like_suffixes():
    # "Services" is a legitimate distinguishing word, not a legal suffix -
    # stripping it would collide "Amazon Web Services" with "Amazon Web".
    assert normalize_employer_name("Amazon Web Services, Inc.") == "amazon web services"


def test_known_employer_aliases_amazon():
    aliases = known_employer_aliases("amazon")
    assert "amazon web services" in aliases
    assert "amazon com services" in aliases


def test_known_employer_aliases_resolves_from_alias_not_just_brand():
    aliases = known_employer_aliases(normalize_employer_name("Amazon Web Services, Inc."))
    assert "amazon" in aliases


def test_known_employer_aliases_empty_for_unknown_company():
    assert known_employer_aliases("some random company") == ()


def test_fuzzy_match_employer_finds_close_variant():
    candidates = ["acme corp global", "beta industries", "gamma llc"]
    assert fuzzy_match_employer("acme corp globl", candidates) == "acme corp global"


def test_fuzzy_match_employer_returns_none_below_threshold():
    candidates = ["completely different company"]
    assert fuzzy_match_employer("acme corp", candidates) is None


def test_fuzzy_match_employer_returns_none_for_empty_candidates():
    assert fuzzy_match_employer("acme corp", []) is None
