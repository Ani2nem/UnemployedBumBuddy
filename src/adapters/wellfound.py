"""Wellfound (AngelList Talent) adapter.

Spike finding: Wellfound's job-listing surfaces (`/jobs`, `/role/r/{slug}`,
`/company/{slug}/jobs`) are gated by DataDome, an active anti-bot/CAPTCHA service -
confirmed live: a plain headless Playwright request to any of those paths gets
served a DataDome CAPTCHA challenge instead of page content, while the marketing
homepage loads fine. That's a deliberate technical control aimed at blocking
automated traffic (unlike Google's robots.txt, which is an advisory signal), so this
adapter does not attempt fingerprint spoofing, proxy rotation, or CAPTCHA solving to
get around it.

Per your direction, this adapter instead authenticates the same way the Handshake
adapter is planned to: by reusing your own logged-in browser session, via
Playwright's standard `storage_state` mechanism (cookies + localStorage captured
from a real login). You accept the account-risk tradeoff, same as Handshake. Nothing
here automates the login/credential flow itself - `storage_state` must be produced
out-of-band (e.g. `playwright codegen` or a one-off script that logs in interactively
and calls `context.storage_state(path=...)`), then handed to this adapter.

IMPORTANT - unverified extraction: because I don't hold Wellfound credentials, I
could not spike the authenticated page's real DOM/JSON shape this session (unlike
amazon.py/google.py, where every selector was checked against live responses).
`_extract_job_cards` below is written defensively - it first looks for an embedded
Next.js/React state blob (`__NEXT_DATA__` or `__APOLLO_STATE__`, both common on
modern job boards and, if present, far more stable than hashed CSS classes), and
falls back to a generic pattern match on job-detail links otherwise. Treat the first
real run against an authenticated session as a validation pass: log a sample of
`raw_metadata` and confirm the fields line up before trusting this in production.

Apply links are resolved through `ats_redirect.py` - Wellfound listings frequently
route "Apply" through the company's real ATS (Greenhouse/Lever/Ashby), so we prefer
that canonical source over whatever text Wellfound's own page renders.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import httpx
from playwright.sync_api import Page, Route, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from adapters.ats_redirect import enrich_via_ats
from shared.contracts import JobPosting

logger = logging.getLogger(__name__)

_BASE_URL = "https://wellfound.com"
_DEFAULT_ROLES = ("software-engineer",)
_NAV_TIMEOUT_MS = 20_000
_MAX_SCROLLS = 8
_JOB_LINK_RE = re.compile(r"^/jobs/(\d+)")

_JSON_BLOB_JS = """
() => {
  const nextData = document.getElementById('__NEXT_DATA__');
  if (nextData) return nextData.textContent;
  const apollo = document.getElementById('__APOLLO_STATE__');
  if (apollo) return apollo.textContent;
  return null;
}
"""

_LINK_SCAN_JS = """
() => Array.from(document.querySelectorAll('a[href]')).map(a => ({
  href: a.getAttribute('href'),
  text: a.innerText ? a.innerText.trim() : '',
}))
"""


class WellfoundAdapter:
    source_name = "wellfound"

    def __init__(
        self,
        storage_state: str | dict[str, Any] | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        """`storage_state` is a Playwright storage-state path or dict captured from
        an authenticated Wellfound session - required, since the job surfaces 404
        into a CAPTCHA otherwise. `http_client` is used for ATS-redirect lookups."""
        self._storage_state = storage_state
        self._http_client = http_client or httpx.Client(timeout=20.0)

    def fetch_new_postings(self, since: datetime | None, filters: dict[str, Any]) -> list[JobPosting]:
        """`filters` supports:
        - `roles`: list[str] of Wellfound role-search slugs (e.g. "software-engineer"),
          each fetched via `/role/r/{slug}`. Defaults to a broad software-role slug.

        `since` is accepted for Protocol compliance; Wellfound listings don't reliably
        expose a machine-readable posted date at the search level, so (like Google)
        recency is left to the pipeline's SeenJobs dedup rather than filtered here.
        """
        if self._storage_state is None:
            raise RuntimeError(
                "WellfoundAdapter requires an authenticated storage_state - "
                "job pages are DataDome-gated for anonymous/automated traffic."
            )

        roles: list[str] = filters.get("roles") or list(_DEFAULT_ROLES)
        return self._run(roles)

    def _run(self, roles: list[str]) -> list[JobPosting]:
        postings: dict[str, JobPosting] = {}

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(storage_state=self._storage_state)
                context.route("**/*", _block_heavy_resources)
                try:
                    page = context.new_page()
                    page.set_default_navigation_timeout(_NAV_TIMEOUT_MS)

                    for role in roles:
                        for card in self._search(page, role):
                            posting = self._card_to_job_posting(card)
                            if posting is not None:
                                postings[posting.job_key] = posting

                    page.close()
                finally:
                    context.close()
            finally:
                browser.close()

        return list(postings.values())

    def _search(self, page: Page, role: str) -> list[dict[str, Any]]:
        url = f"{_BASE_URL}/role/r/{role}"
        try:
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle", timeout=_NAV_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            logger.warning("wellfound search timed out for role=%r", role)
            return []

        if "captcha-delivery.com" in page.content():
            logger.error(
                "wellfound served a DataDome CAPTCHA for role=%r despite storage_state - "
                "session is likely expired or invalid; re-capture it",
                role,
            )
            return []

        cards = self._extract_from_json_blob(page)
        if cards:
            return cards
        return self._extract_from_links(page, url)

    def _extract_from_json_blob(self, page: Page) -> list[dict[str, Any]]:
        raw = page.evaluate(_JSON_BLOB_JS)
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        # Shape is unverified without a live authenticated session - walk the blob
        # looking for dict entries that look like job listings rather than assuming
        # a fixed path, so this survives minor structural differences.
        return list(_find_job_like_dicts(data))

    def _extract_from_links(self, page: Page, search_url: str) -> list[dict[str, Any]]:
        seen_ids: set[str] = set()
        cards: list[dict[str, Any]] = []

        for _ in range(_MAX_SCROLLS):
            links = page.evaluate(_LINK_SCAN_JS)
            new_count = 0
            for link in links:
                href = link.get("href") or ""
                match = _JOB_LINK_RE.match(href)
                if not match:
                    continue
                job_id = match.group(1)
                if job_id in seen_ids:
                    continue
                seen_ids.add(job_id)
                new_count += 1
                cards.append(
                    {
                        "job_id": job_id,
                        "title": link.get("text") or "",
                        "url": urljoin(search_url, href),
                    }
                )

            if new_count == 0:
                break
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(1000)

        return cards

    def _card_to_job_posting(self, card: dict[str, Any]) -> JobPosting | None:
        job_id = str(card.get("job_id") or card.get("id") or "")
        if not job_id:
            return None

        url = card.get("url") or f"{_BASE_URL}/jobs/{job_id}"
        title = card.get("title") or ""
        company = card.get("company") or card.get("company_name") or ""
        location = card.get("location") or ""
        apply_url = card.get("apply_url") or url

        ats_posting = enrich_via_ats(self._http_client, apply_url)

        return JobPosting(
            source=self.source_name,
            external_id=job_id,
            title=(ats_posting.title if ats_posting else title) or title,
            company=company or (ats_posting.company if ats_posting else "") or "",
            location=(ats_posting.location if ats_posting else location) or location,
            country_code=card.get("country_code") or "",
            remote_flag=bool(ats_posting.remote_flag) if ats_posting else bool(card.get("remote_flag")),
            url=url,
            description_text=(ats_posting.description_text if ats_posting else card.get("description", "")) or "",
            posted_at=ats_posting.posted_at if ats_posting else None,
            salary_text=(ats_posting.salary_text if ats_posting else card.get("salary_text")),
            ats_platform=ats_posting.platform if ats_posting else "none",
            ats_board_token=ats_posting.board_token if ats_posting else None,
            ats_job_id=ats_posting.job_id if ats_posting else None,
            raw_metadata=card,
        )


def _find_job_like_dicts(node: Any, _depth: int = 0) -> Any:
    if _depth > 12:
        return
    if isinstance(node, dict):
        keys = {k.lower() for k in node}
        if {"title", "id"} <= keys and (
            "company" in keys or "companyname" in keys or "location" in keys
        ):
            yield node
            return
        for value in node.values():
            yield from _find_job_like_dicts(value, _depth + 1)
    elif isinstance(node, list):
        for item in node:
            yield from _find_job_like_dicts(item, _depth + 1)


def _block_heavy_resources(route: Route) -> None:
    if route.request.resource_type in {"image", "stylesheet", "font", "media"}:
        route.abort()
    else:
        route.continue_()


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    from shared.serialize import job_posting_to_dict

    since_raw = event.get("since")
    since = datetime.fromisoformat(since_raw) if since_raw else None
    filters = event.get("filters") or {}
    storage_state = event.get("storage_state")

    adapter = WellfoundAdapter(storage_state=storage_state)
    postings = adapter.fetch_new_postings(since, filters)
    return {"source": WellfoundAdapter.source_name, "postings": [job_posting_to_dict(p) for p in postings]}
