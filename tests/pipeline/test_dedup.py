from __future__ import annotations

import pytest

from pipeline import dedup


def test_try_claim_new_first_call_wins(seen_jobs_table, make_posting):
    posting = make_posting()
    assert dedup.try_claim_new(posting) is True
    assert dedup.try_claim_new(posting) is False


def test_is_seen(seen_jobs_table, make_posting):
    posting = make_posting()
    assert dedup.is_seen(posting.job_key) is False
    dedup.try_claim_new(posting)
    assert dedup.is_seen(posting.job_key) is True


def test_filter_new_splits_new_from_seen(seen_jobs_table, make_posting):
    already_seen = make_posting(external_id="1")
    dedup.try_claim_new(already_seen)

    fresh = make_posting(external_id="2")
    result = dedup.filter_new([already_seen, fresh])

    assert [p.external_id for p in result] == ["2"]


def test_update_status_sets_status_and_extra_attrs(seen_jobs_table, make_posting):
    posting = make_posting()
    dedup.try_claim_new(posting)

    dedup.update_status(posting.job_key, "FILTERED_OUT", reason="visa_lca_no_evidence")

    item = dedup.get_seen_job(posting.job_key)
    assert item["status"] == "FILTERED_OUT"
    assert item["reason"] == "visa_lca_no_evidence"


def test_update_status_rejects_unknown_status(seen_jobs_table, make_posting):
    posting = make_posting()
    dedup.try_claim_new(posting)

    with pytest.raises(ValueError):
        dedup.update_status(posting.job_key, "NOT_A_REAL_STATUS")


def test_get_seen_job_returns_none_when_absent(seen_jobs_table):
    assert dedup.get_seen_job("amazon#does-not-exist") is None
