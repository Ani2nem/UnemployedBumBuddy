from __future__ import annotations

from datetime import date

import pytest

from pipeline import visa_level_filter as vlf
from pipeline.models import CandidateProfile, ExperienceLevel


@pytest.fixture
def profile():
    return CandidateProfile(profile_id="me", current_level=ExperienceLevel.MID)


# --- location ---


def test_check_location_passes_for_target_country(make_posting, profile):
    posting = make_posting(country_code="US")
    assert vlf.check_location(posting, profile) is True


def test_check_location_fails_for_other_country(make_posting, profile):
    posting = make_posting(country_code="IN")
    assert vlf.check_location(posting, profile) is False


# --- visa keyword scan ---


@pytest.mark.parametrize(
    "text",
    [
        "We do not sponsor visas.",
        "We are unable to sponsor employment visas at this time.",
        "Candidates must be authorized to work in the US without sponsorship.",
        "This role requires an active security clearance.",
    ],
)
def test_scan_visa_keywords_detects_explicit_negative(text):
    assert vlf.scan_visa_keywords(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "This role offers visa sponsorship available for qualified candidates.",
        "We will sponsor H-1B visas for the right candidate.",
    ],
)
def test_scan_visa_keywords_detects_explicit_positive(text):
    assert vlf.scan_visa_keywords(text) is True


def test_scan_visa_keywords_silent_when_unmentioned():
    assert vlf.scan_visa_keywords("Come build great software with us.") is None


# --- title level classification ---


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Software Development Engineer II", ExperienceLevel.MID),
        ("Senior Software Engineer", ExperienceLevel.SENIOR),
        ("Staff Software Engineer", ExperienceLevel.STAFF),
        ("Principal Engineer", ExperienceLevel.PRINCIPAL),
        ("Distinguished Engineer", ExperienceLevel.DISTINGUISHED),
        ("New Grad Software Engineer", ExperienceLevel.ENTRY),
        ("Data Engineer III", ExperienceLevel.SENIOR),
    ],
)
def test_classify_title_level(title, expected):
    assert vlf.classify_title_level(title) == expected


def test_classify_title_level_returns_none_for_generic_title():
    assert vlf.classify_title_level("Software Engineer") is None


# --- wage level + band ---


def test_wage_level_to_experience_maps_roman_numerals():
    assert vlf.wage_level_to_experience("II") == ExperienceLevel.MID
    assert vlf.wage_level_to_experience(None) is None
    assert vlf.wage_level_to_experience("not-a-level") is None


def test_level_in_band_allows_up_to_two_above():
    assert vlf.level_in_band(ExperienceLevel.STAFF, ExperienceLevel.MID) is True  # +2
    assert vlf.level_in_band(ExperienceLevel.SENIOR, ExperienceLevel.MID) is True  # +1


def test_level_in_band_excludes_below():
    assert vlf.level_in_band(ExperienceLevel.ENTRY, ExperienceLevel.MID) is False


def test_level_in_band_excludes_dramatically_above():
    assert vlf.level_in_band(ExperienceLevel.PRINCIPAL, ExperienceLevel.MID) is False  # +3


# --- salary parsing ---


@pytest.mark.parametrize(
    "text,expected",
    [
        ("$120,000 - $150,000", (120000.0, 150000.0)),
        ("$120K - $150K", (120000.0, 150000.0)),
        ("150000 to 120000", (120000.0, 150000.0)),
        (None, None),
        ("Competitive", None),
    ],
)
def test_parse_salary_range(text, expected):
    assert vlf.parse_salary_range(text) == expected


# --- LCA matching ---


def test_evaluate_lca_match_no_filings_is_no_evidence(make_posting):
    posting = make_posting()
    outcome, filing = vlf.evaluate_lca_match(posting, [])
    assert outcome == "no_evidence"
    assert filing is None


def test_evaluate_lca_match_matches_on_wage_level(make_posting):
    from pipeline.models import SponsorFiling

    posting = make_posting(title="Software Development Engineer II")
    filing = SponsorFiling(
        employer_normalized="amazon com services",
        decision_date=date(2025, 6, 1),
        case_id="C1",
        job_title="Software Development Engineer II",
        soc_title="Software Developers",
        wage_from=130000,
        wage_to=160000,
        pw_wage_level="II",
    )
    outcome, matched = vlf.evaluate_lca_match(posting, [filing], title_similarity_fn=None)
    assert outcome == "match"
    assert matched is filing


def test_evaluate_lca_match_ruled_out_by_salary_mismatch(make_posting):
    from pipeline.models import SponsorFiling

    posting = make_posting(title="Software Development Engineer II", salary_text="$400,000 - $450,000")
    filing = SponsorFiling(
        employer_normalized="amazon com services",
        decision_date=date(2025, 6, 1),
        case_id="C1",
        job_title="Software Development Engineer II",
        soc_title="Software Developers",
        wage_from=130000,
        wage_to=160000,
        pw_wage_level="II",
    )
    outcome, matched = vlf.evaluate_lca_match(posting, [filing], title_similarity_fn=None)
    assert outcome == "ruled_out"
    assert matched is None


def test_evaluate_lca_match_ambiguous_when_level_undetermined(make_posting):
    from pipeline.models import SponsorFiling

    posting = make_posting(title="Software Engineer")  # no level signal
    filing = SponsorFiling(
        employer_normalized="amazon com services",
        decision_date=date(2025, 6, 1),
        case_id="C1",
        job_title="Software Development Engineer II",
        soc_title="Software Developers",
        wage_from=None,
        wage_to=None,
        pw_wage_level=None,
    )
    outcome, matched = vlf.evaluate_lca_match(posting, [filing], title_similarity_fn=None)
    assert outcome == "ambiguous"
    assert matched is filing


def test_fetch_sponsor_filings_finds_alias_and_respects_lookback(sponsor_history_table):
    sponsor_history_table.put_item(
        Item={
            "employer_normalized": "amazon com services",
            "decision_date_case_id": "2025-06-01#RECENT",
            "job_title": "SDE II",
            "soc_title": "Software Developers",
            "wage_from": 130000,
            "wage_to": 160000,
            "pw_wage_level": "II",
        }
    )
    sponsor_history_table.put_item(
        Item={
            "employer_normalized": "amazon com services",
            "decision_date_case_id": "2020-01-01#OLD",
            "job_title": "SDE II",
            "soc_title": "Software Developers",
            "wage_from": 100000,
            "wage_to": 120000,
            "pw_wage_level": "II",
        }
    )

    filings = vlf.fetch_sponsor_filings("Amazon.com Services LLC", as_of=date(2026, 7, 23))
    assert len(filings) == 1
    assert filings[0].case_id == "RECENT"


def test_fetch_sponsor_filings_resolves_brand_via_alias_table(sponsor_history_table):
    # Posting says "Amazon Web Services" but the DOL filer is under the
    # canonical alias-table brand key's normalized filer-name variant.
    sponsor_history_table.put_item(
        Item={
            "employer_normalized": "amazon web services",
            "decision_date_case_id": "2025-06-01#C1",
            "job_title": "SDE II",
            "soc_title": "Software Developers",
            "wage_from": 130000,
            "wage_to": 160000,
            "pw_wage_level": "II",
        }
    )
    filings = vlf.fetch_sponsor_filings("Amazon Web Services, Inc.", as_of=date(2026, 7, 23))
    assert len(filings) == 1


# --- end-to-end orchestration ---


def test_filter_posting_passes_on_explicit_positive_keyword_and_good_level(make_posting, profile, sponsor_history_table):
    posting = make_posting(
        title="Software Development Engineer II",
        description_text="This role offers visa sponsorship available for qualified candidates.",
    )
    result = vlf.filter_posting(posting, profile, as_of=date(2026, 7, 23), title_similarity_fn=None)
    assert result.passed is True
    assert result.stage == "passed"


def test_filter_posting_excludes_on_explicit_negative_keyword(make_posting, profile, sponsor_history_table):
    posting = make_posting(description_text="We do not sponsor visas.")
    result = vlf.filter_posting(posting, profile, as_of=date(2026, 7, 23), title_similarity_fn=None)
    assert result.passed is False
    assert result.stage == "visa_keyword_exclude"


def test_filter_posting_excludes_when_no_lca_evidence(make_posting, profile, sponsor_history_table):
    posting = make_posting(company="RandoCorp Inc", description_text="Nothing about visas here.")
    result = vlf.filter_posting(posting, profile, as_of=date(2026, 7, 23), title_similarity_fn=None)
    assert result.passed is False
    assert result.stage == "visa_lca_no_evidence"


def test_filter_posting_passes_via_lca_match(make_posting, profile, sponsor_history_table):
    sponsor_history_table.put_item(
        Item={
            "employer_normalized": "amazon com services",
            "decision_date_case_id": "2025-06-01#C1",
            "job_title": "Software Development Engineer II",
            "soc_title": "Software Developers",
            "wage_from": 130000,
            "wage_to": 160000,
            "pw_wage_level": "II",
        }
    )
    posting = make_posting(
        title="Software Development Engineer II",
        description_text="Nothing about visas here.",
        salary_text="$130,000 - $160,000",
    )
    result = vlf.filter_posting(posting, profile, as_of=date(2026, 7, 23), title_similarity_fn=None)
    assert result.passed is True
    assert result.visa_likely is True


def test_filter_posting_excludes_level_too_senior(make_posting, profile, sponsor_history_table):
    posting = make_posting(
        title="Principal Engineer",
        description_text="This role offers visa sponsorship available.",
    )
    result = vlf.filter_posting(posting, profile, as_of=date(2026, 7, 23), title_similarity_fn=None)
    assert result.passed is False
    assert result.stage == "experience_level"


def test_filter_posting_excludes_location(make_posting, profile, sponsor_history_table):
    posting = make_posting(country_code="IN")
    result = vlf.filter_posting(posting, profile, as_of=date(2026, 7, 23), title_similarity_fn=None)
    assert result.passed is False
    assert result.stage == "location"


def test_filter_posting_defers_to_llm_when_ambiguous(make_posting, profile, sponsor_history_table):
    # A filing exists (so this isn't a hard "no evidence" exclude) but carries
    # no pw_wage_level, and the posting's title has no level signal either -
    # genuinely ambiguous, with title-embedding similarity disabled for the test.
    sponsor_history_table.put_item(
        Item={
            "employer_normalized": "amazon com services",
            "decision_date_case_id": "2025-06-01#C1",
            "job_title": "Some Other Role",
            "soc_title": "Software Developers",
            "wage_from": None,
            "wage_to": None,
            "pw_wage_level": None,
        }
    )
    posting = make_posting(title="Software Engineer", description_text="Build things.")

    def fake_llm(system, user):
        assert "Software Engineer" in user
        return {"visa_likely": True, "level_match": True, "rationale": "reasonable fit"}

    result = vlf.filter_posting(
        posting, profile, as_of=date(2026, 7, 23), title_similarity_fn=None, llm_invoke=fake_llm
    )
    assert result.passed is True
    assert result.stage == "llm_ambiguous"
