"""Amazon.jobs adapter.

Spike finding: `https://www.amazon.jobs/en/search.json` is the same unauthenticated
JSON endpoint the amazon.jobs search frontend itself calls (confirmed live - plain
GET with a browser User-Agent, no cookies/auth/CSRF token required, returns full job
records including id, title, company, location, description, and posting date). So
this is a lightweight `httpx` adapter, no headless browser. If Amazon ever locks this
down (auth requirement, 403s, breaking schema change), fall back to Playwright per
the workstream's default assumption - nothing else in this module assumes the JSON
endpoint stays open.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from adapters._htmlutil import strip_html
from shared.contracts import JobPosting

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://www.amazon.jobs/en/search.json"
_BASE_URL = "https://www.amazon.jobs"
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_PAGE_SIZE = 100
_MAX_PAGES = 20  # safety cap: 2,000 postings per query per run
_DEFAULT_QUERIES = ("software engineer",)
_DEFAULT_COUNTRY_CODES = ("USA",)

# Amazon.jobs' `country_code` is ISO 3166-1 alpha-3; JobPosting.country_code is
# alpha-2. Covers the countries Amazon actually posts roles in, not the full ISO list.
_COUNTRY_ALPHA3_TO_ALPHA2 = {
    "USA": "US", "CAN": "CA", "GBR": "GB", "IRL": "IE", "DEU": "DE", "FRA": "FR",
    "ESP": "ES", "ITA": "IT", "NLD": "NL", "POL": "PL", "ROU": "RO", "CZE": "CZ",
    "LUX": "LU", "CHE": "CH", "SWE": "SE", "AUT": "AT", "BEL": "BE", "PRT": "PT",
    "IND": "IN", "CHN": "CN", "JPN": "JP", "KOR": "KR", "SGP": "SG", "AUS": "AU",
    "NZL": "NZ", "ISR": "IL", "ARE": "AE", "ZAF": "ZA", "BRA": "BR", "MEX": "MX",
    "CRI": "CR", "COL": "CO", "ARG": "AR", "CHL": "CL", "PHL": "PH", "EGY": "EG",
    "JOR": "JO", "SAU": "SA", "TUR": "TR", "DNK": "DK", "FIN": "FI", "NOR": "NO",
}


class AmazonJobsAdapter:
    source_name = "amazon"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(headers={"User-Agent": _USER_AGENT}, timeout=20.0)

    def fetch_new_postings(self, since: datetime | None, filters: dict[str, Any]) -> list[JobPosting]:
        """`filters` supports:
        - `queries`: list[str] of `base_query` search terms (defaults to a broad
          software-role query; Amazon's search endpoint has no "list everything"
          mode, so a query term is required).
        - `country_codes`: list[str] of ISO alpha-3 country codes to restrict the
          source-level location filter to (defaults to USA).
        """
        queries: list[str] = filters.get("queries") or list(_DEFAULT_QUERIES)
        country_codes: list[str] = filters.get("country_codes") or list(_DEFAULT_COUNTRY_CODES)

        postings: dict[str, JobPosting] = {}
        for query in queries:
            for posting in self._search(query, country_codes, since):
                postings[posting.job_key] = posting
        return list(postings.values())

    def _search(
        self, query: str, country_codes: list[str], since: datetime | None
    ) -> list[JobPosting]:
        results: list[JobPosting] = []
        offset = 0

        for _ in range(_MAX_PAGES):
            params = [("base_query", query), ("result_limit", str(_PAGE_SIZE)), ("offset", str(offset))]
            params.extend(("normalized_country_code[]", code) for code in country_codes)

            try:
                response = self._client.get(_SEARCH_URL, params=params)
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                logger.warning("amazon.jobs search failed for query=%r offset=%d: %s", query, offset, exc)
                break

            jobs = data.get("jobs") or []
            if not jobs:
                break

            for raw in jobs:
                posting = self._to_job_posting(raw)
                if since is not None and posting.posted_at is not None and posting.posted_at < since:
                    continue
                results.append(posting)

            if len(jobs) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE

        return results

    def _to_job_posting(self, raw: dict[str, Any]) -> JobPosting:
        external_id = str(raw.get("id_icims") or raw.get("id"))
        job_path = raw.get("job_path") or ""
        url = f"{_BASE_URL}{job_path}" if job_path else f"{_BASE_URL}/en/jobs/{external_id}"

        country_alpha3 = (raw.get("country_code") or "").upper()
        country_code = _COUNTRY_ALPHA3_TO_ALPHA2.get(country_alpha3, country_alpha3[:2] or "US")

        location_parts = [p for p in (raw.get("city"), raw.get("state")) if p]
        location = ", ".join(location_parts) or raw.get("normalized_location") or raw.get("location", "")

        description = "\n\n".join(
            strip_html(part)
            for part in (
                raw.get("description"),
                raw.get("basic_qualifications"),
                raw.get("preferred_qualifications"),
            )
            if part
        )

        return JobPosting(
            source=self.source_name,
            external_id=external_id,
            title=(raw.get("title") or "").strip(),
            company=raw.get("company_name") or "Amazon",
            location=location,
            country_code=country_code,
            remote_flag=_is_remote(raw, location),
            url=url,
            description_text=description,
            posted_at=_parse_posted_date(raw.get("posted_date")),
            salary_text=None,
            ats_platform="none",
            ats_board_token=None,
            ats_job_id=None,
            raw_metadata=raw,
        )


def _is_remote(raw: dict[str, Any], location: str) -> bool:
    locations_raw = raw.get("locations")
    if locations_raw:
        try:
            first = json.loads(locations_raw[0])
        except (json.JSONDecodeError, IndexError, TypeError):
            first = None
        if first and first.get("type") == "REMOTE":
            return True
    return "remote" in location.lower()


def _parse_posted_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%B %d, %Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    from shared.serialize import job_posting_to_dict

    since_raw = event.get("since")
    since = datetime.fromisoformat(since_raw) if since_raw else None
    filters = event.get("filters") or {}

    adapter = AmazonJobsAdapter()
    postings = adapter.fetch_new_postings(since, filters)
    return {"source": AmazonJobsAdapter.source_name, "postings": [job_posting_to_dict(p) for p in postings]}
