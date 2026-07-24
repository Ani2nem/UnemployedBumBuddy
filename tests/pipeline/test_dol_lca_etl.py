from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from pipeline import dol_lca_etl as etl

_CUTOFF = date(2024, 1, 1)


def _row(**overrides) -> dict:
    base = {
        "CASE_NUMBER": "CASE001",
        "CASE_STATUS": "Certified",
        "DECISION_DATE": "2025-06-01",
        "EMPLOYER_NAME": "Amazon.com Services LLC",
        "JOB_TITLE": "Software Development Engineer II",
        "SOC_TITLE": "Software Developers",
        "PW_WAGE_LEVEL": "II",
        "WAGE_RATE_OF_PAY_FROM_1": "130000",
        "WAGE_RATE_OF_PAY_TO_1": "160000",
        "WAGE_UNIT_OF_PAY_1": "Year",
    }
    base.update(overrides)
    return base


def test_row_to_sponsor_record_happy_path():
    record = etl.row_to_sponsor_record(_row(), cutoff=_CUTOFF)
    assert record["employer_normalized"] == "amazon com services"
    assert record["decision_date_case_id"] == "2025-06-01#CASE001"
    assert record["wage_from"] == Decimal("130000.0")
    assert record["wage_to"] == Decimal("160000.0")
    assert record["pw_wage_level"] == "II"


def test_row_to_sponsor_record_drops_non_certified():
    assert etl.row_to_sponsor_record(_row(CASE_STATUS="Denied"), cutoff=_CUTOFF) is None
    assert etl.row_to_sponsor_record(_row(CASE_STATUS="Withdrawn"), cutoff=_CUTOFF) is None


def test_row_to_sponsor_record_drops_before_cutoff():
    assert etl.row_to_sponsor_record(_row(DECISION_DATE="2020-01-01"), cutoff=_CUTOFF) is None


def test_row_to_sponsor_record_drops_missing_required_fields():
    assert etl.row_to_sponsor_record(_row(CASE_NUMBER=""), cutoff=_CUTOFF) is None
    assert etl.row_to_sponsor_record(_row(EMPLOYER_NAME=""), cutoff=_CUTOFF) is None


def test_row_to_sponsor_record_annualizes_hourly_wage():
    record = etl.row_to_sponsor_record(
        _row(WAGE_RATE_OF_PAY_FROM_1="60", WAGE_RATE_OF_PAY_TO_1="70", WAGE_UNIT_OF_PAY_1="Hour"),
        cutoff=_CUTOFF,
    )
    assert record["wage_from"] == Decimal("124800.0")
    assert record["wage_to"] == Decimal("145600.0")


def test_row_to_sponsor_record_prefers_dba_over_legal_name():
    record = etl.row_to_sponsor_record(
        _row(EMPLOYER_NAME="Small Startup LLC", EMPLOYER_BUSINESS_DBA="SmallCo"), cutoff=_CUTOFF
    )
    assert record["employer_normalized"] == "smallco"


def test_row_to_sponsor_record_handles_missing_wage_gracefully():
    record = etl.row_to_sponsor_record(
        _row(WAGE_RATE_OF_PAY_FROM_1="", WAGE_RATE_OF_PAY_TO_1=""), cutoff=_CUTOFF
    )
    assert record["wage_from"] is None
    assert record["wage_to"] is None


def test_run_etl_loads_only_qualifying_rows_from_csv(tmp_path: Path, sponsor_history_table):
    csv_path = tmp_path / "fake_lca.csv"
    csv_path.write_text(
        "CASE_NUMBER,CASE_STATUS,DECISION_DATE,EMPLOYER_NAME,EMPLOYER_BUSINESS_DBA,JOB_TITLE,SOC_TITLE,"
        "PW_WAGE_LEVEL,WAGE_RATE_OF_PAY_FROM_1,WAGE_RATE_OF_PAY_TO_1,WAGE_UNIT_OF_PAY_1\n"
        "CASE001,Certified,2025-06-01,Amazon.com Services LLC,,SDE II,Software Developers,II,130000,160000,Year\n"
        "CASE002,Denied,2025-06-01,Amazon.com Services LLC,,SDE III,Software Developers,III,150000,180000,Year\n"
        "CASE003,Certified,2020-01-01,Amazon.com Services LLC,,Old Filing,Software Developers,II,100000,120000,Year\n"
        "CASE004,Certified,2025-07-01,Small Startup LLC,SmallCo,Backend Engineer,Software Developers,II,60,70,Hour\n"
    )

    count = etl.run_etl(csv_path, as_of=date(2026, 7, 23))
    assert count == 2

    items = {item["decision_date_case_id"]: item for item in sponsor_history_table.scan()["Items"]}
    assert "2025-06-01#CASE001" in items
    assert "2025-07-01#CASE004" in items
    assert items["2025-07-01#CASE004"]["employer_normalized"] == "smallco"
