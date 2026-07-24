from __future__ import annotations

import os

import boto3
import pytest
from moto import mock_aws

from shared.contracts import JobPosting
from shared.tables import (
    COMPANY_RESEARCH_CACHE_TABLE,
    PROJECTS_TABLE,
    SEEN_JOBS_TABLE,
    SPONSOR_HISTORY_TABLE,
    STYLE_EXAMPLES_TABLE,
)


@pytest.fixture(autouse=True)
def aws_credentials():
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
    os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"


@pytest.fixture(autouse=True)
def _reset_dynamo_resource_cache():
    from pipeline import dynamo

    dynamo._resource.cache_clear()
    yield
    dynamo._resource.cache_clear()


@pytest.fixture
def dynamodb():
    with mock_aws():
        yield boto3.resource("dynamodb", region_name="us-east-1")


def _create_table(client, table_name: str, pk: str, sk: str | None = None):
    key_schema = [{"AttributeName": pk, "KeyType": "HASH"}]
    attr_defs = [{"AttributeName": pk, "AttributeType": "S"}]
    if sk:
        key_schema.append({"AttributeName": sk, "KeyType": "RANGE"})
        attr_defs.append({"AttributeName": sk, "AttributeType": "S"})
    client.create_table(
        TableName=table_name,
        KeySchema=key_schema,
        AttributeDefinitions=attr_defs,
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def seen_jobs_table(dynamodb):
    _create_table(dynamodb.meta.client, SEEN_JOBS_TABLE, "job_key")
    return dynamodb.Table(SEEN_JOBS_TABLE)


@pytest.fixture
def sponsor_history_table(dynamodb):
    _create_table(dynamodb.meta.client, SPONSOR_HISTORY_TABLE, "employer_normalized", "decision_date_case_id")
    return dynamodb.Table(SPONSOR_HISTORY_TABLE)


@pytest.fixture
def company_research_cache_table(dynamodb):
    _create_table(dynamodb.meta.client, COMPANY_RESEARCH_CACHE_TABLE, "company_normalized")
    return dynamodb.Table(COMPANY_RESEARCH_CACHE_TABLE)


@pytest.fixture
def projects_table(dynamodb):
    _create_table(dynamodb.meta.client, PROJECTS_TABLE, "project_id")
    return dynamodb.Table(PROJECTS_TABLE)


@pytest.fixture
def style_examples_table(dynamodb):
    _create_table(dynamodb.meta.client, STYLE_EXAMPLES_TABLE, "example_id")
    return dynamodb.Table(STYLE_EXAMPLES_TABLE)


def _make_posting(**overrides) -> JobPosting:
    defaults = {
        "source": "amazon",
        "external_id": "1",
        "title": "Software Development Engineer II",
        "company": "Amazon.com Services LLC",
        "location": "Seattle, WA",
        "country_code": "US",
        "remote_flag": False,
        "url": "https://example.com/job/1",
        "description_text": "Build cool stuff.",
        "posted_at": None,
        "salary_text": None,
        "ats_platform": "none",
        "ats_board_token": None,
        "ats_job_id": None,
    }
    defaults.update(overrides)
    return JobPosting(**defaults)


@pytest.fixture
def make_posting():
    return _make_posting
