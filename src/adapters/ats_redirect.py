"""Detect when a scraped "Apply" URL redirects to Greenhouse, Lever, or Ashby, and
re-fetch the canonical posting from that ATS's own public Job Board API instead of
trusting the scraped landing page.

Used by the Wellfound adapter today; written generically (`resolve_apply_url` +
`fetch_canonical_posting`, or the one-shot `enrich_via_ats`) so any future adapter
that scrapes a company/job-board page can reuse it.

API shapes were verified live against real boards before writing this:
- Greenhouse `GET /v1/boards/{token}/jobs/{id}` returns `company_name`, `title`,
  `location.name`, `absolute_url`, `updated_at`, `content` (HTML).
- Lever `GET /v0/postings/{company}?mode=json` returns a JSON array of every open
  posting for that company (no single-item endpoint) - fields include `text` (title),
  `categories.location`, `country`, `createdAt` (epoch ms), `hostedUrl`,
  `workplaceType`, `descriptionPlain`.
- Ashby `GET /posting-api/job-board/{clientname}` returns `{"jobs": [...]}` (not
  `jobPostings`, despite older docs/specs suggesting that key) - fields include
  `title`, `location`, `isRemote`, `publishedAt`, `jobUrl`, `descriptionPlain`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from adapters._htmlutil import strip_html
from shared.contracts import AtsPlatform

_GREENHOUSE_HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io"}
_LEVER_HOST = "jobs.lever.co"
_ASHBY_HOST = "jobs.ashbyhq.com"

_GREENHOUSE_PATH_RE = re.compile(r"^/(?P<board_token>[^/]+)/jobs/(?P<job_id>\d+)")
_UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_LEVER_PATH_RE = re.compile(rf"^/(?P<company>[^/]+)/(?P<job_id>{_UUID_RE})")
_ASHBY_PATH_RE = re.compile(rf"^/(?P<clientname>[^/]+)/(?P<job_id>{_UUID_RE})")


@dataclass(frozen=True)
class AtsMatch:
    platform: AtsPlatform
    board_token: str
    job_id: str


@dataclass(frozen=True)
class AtsPosting:
    """Canonical fields pulled from the ATS's own API.

    `company` is None for Lever/Ashby (neither API returns a clean display name for
    the employer, just the URL slug) - callers that already know the company name
    from the page they scraped should keep their own value rather than overwrite it
    with this one.
    """

    platform: AtsPlatform
    board_token: str
    job_id: str
    title: str
    company: str | None
    location: str
    url: str
    description_text: str
    posted_at: datetime | None
    salary_text: str | None
    remote_flag: bool
    raw_metadata: dict[str, Any]


def match_ats_url(url: str) -> AtsMatch | None:
    """Match a URL (post-redirect) against known ATS URL shapes. None if no match."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    path = parsed.path or ""

    if host in _GREENHOUSE_HOSTS:
        m = _GREENHOUSE_PATH_RE.match(path)
        if m:
            return AtsMatch("greenhouse", m.group("board_token"), m.group("job_id"))
    elif host == _LEVER_HOST:
        m = _LEVER_PATH_RE.match(path)
        if m:
            return AtsMatch("lever", m.group("company"), m.group("job_id"))
    elif host == _ASHBY_HOST:
        m = _ASHBY_PATH_RE.match(path)
        if m:
            return AtsMatch("ashby", m.group("clientname"), m.group("job_id"))
    return None


def resolve_apply_url(client: httpx.Client, apply_url: str) -> AtsMatch | None:
    """Follow redirects on a scraped Apply URL and check if it lands on a known ATS."""
    try:
        response = client.get(apply_url, follow_redirects=True, timeout=15.0)
    except httpx.HTTPError:
        return None
    return match_ats_url(str(response.url))


def fetch_canonical_posting(client: httpx.Client, match: AtsMatch) -> AtsPosting | None:
    if match.platform == "greenhouse":
        return _fetch_greenhouse(client, match)
    if match.platform == "lever":
        return _fetch_lever(client, match)
    if match.platform == "ashby":
        return _fetch_ashby(client, match)
    return None


def enrich_via_ats(client: httpx.Client, apply_url: str) -> AtsPosting | None:
    """One-shot: resolve the Apply URL's redirect target, then fetch the canonical
    posting if it lands on a known ATS. None if it doesn't redirect anywhere we
    recognize, or the ATS's API doesn't have that job (delisted, wrong id, etc.)."""
    match = resolve_apply_url(client, apply_url)
    if match is None:
        return None
    return fetch_canonical_posting(client, match)


def _fetch_greenhouse(client: httpx.Client, match: AtsMatch) -> AtsPosting | None:
    url = f"https://boards-api.greenhouse.io/v1/boards/{match.board_token}/jobs/{match.job_id}"
    response = client.get(url, params={"questions": "false"}, timeout=15.0)
    if response.status_code != 200:
        return None
    data = response.json()

    location = ((data.get("location") or {}).get("name") or "").strip()
    return AtsPosting(
        platform="greenhouse",
        board_token=match.board_token,
        job_id=str(data.get("id", match.job_id)),
        title=data.get("title", ""),
        company=data.get("company_name"),
        location=location,
        url=data.get("absolute_url", ""),
        description_text=strip_html(data.get("content", "")),
        posted_at=_parse_iso(data.get("updated_at") or data.get("first_published")),
        salary_text=None,
        remote_flag="remote" in location.lower(),
        raw_metadata=data,
    )


def _fetch_lever(client: httpx.Client, match: AtsMatch) -> AtsPosting | None:
    url = f"https://api.lever.co/v0/postings/{match.board_token}"
    response = client.get(url, params={"mode": "json"}, timeout=15.0)
    if response.status_code != 200:
        return None
    data = response.json()
    if not isinstance(data, list):
        return None

    posting = next((p for p in data if str(p.get("id")) == match.job_id), None)
    if posting is None:
        return None

    categories = posting.get("categories") or {}
    location = categories.get("location") or ""
    workplace_type = (posting.get("workplaceType") or "").lower()
    return AtsPosting(
        platform="lever",
        board_token=match.board_token,
        job_id=str(posting.get("id", match.job_id)),
        title=posting.get("text", ""),
        company=None,
        location=location,
        url=posting.get("hostedUrl", ""),
        description_text=posting.get("descriptionPlain") or strip_html(posting.get("description", "")),
        posted_at=_parse_epoch_millis(posting.get("createdAt")),
        salary_text=None,
        remote_flag=workplace_type == "remote" or "remote" in location.lower(),
        raw_metadata=posting,
    )


def _fetch_ashby(client: httpx.Client, match: AtsMatch) -> AtsPosting | None:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{match.board_token}"
    response = client.get(url, timeout=15.0)
    if response.status_code != 200:
        return None
    data = response.json()
    # Ashby's live API key is "jobs"; keep "jobPostings" as a fallback in case of
    # API drift back toward what older docs described.
    postings = data.get("jobs") or data.get("jobPostings") or []

    posting = next((p for p in postings if str(p.get("id")) == match.job_id), None)
    if posting is None:
        return None

    location = posting.get("location") or ""
    return AtsPosting(
        platform="ashby",
        board_token=match.board_token,
        job_id=str(posting.get("id", match.job_id)),
        title=posting.get("title", ""),
        company=None,
        location=location,
        url=posting.get("jobUrl", ""),
        description_text=posting.get("descriptionPlain") or strip_html(posting.get("descriptionHtml", "")),
        posted_at=_parse_iso(posting.get("publishedAt")),
        salary_text=None,
        remote_flag=bool(posting.get("isRemote")),
        raw_metadata=posting,
    )


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_epoch_millis(value: int | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
