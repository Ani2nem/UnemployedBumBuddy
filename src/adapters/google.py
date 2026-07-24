"""Google Careers adapter.

Spike findings (both confirmed live, not assumed):

1. `careers.google.com` / `google.com/about/careers/applications/jobs/results` is a
   fully client-rendered SPA built on Google's internal WIZ/`batchexecute` RPC
   framework (`boq_corp-hiring-boq-cportal-frontend`). The server-rendered HTML has
   zero job data - no job IDs, no JSON-LD, nothing - it's all fetched by an
   in-page RPC call gated by a session-scoped `f.sid`/`bl` token pair. That's an
   undocumented internal protocol, not a stable public API, so per the workstream's
   default assumption this adapter uses Playwright rather than trying to
   reverse-engineer `batchexecute`.
2. `robots.txt` disallows the `?page=`/`/?page=` paginated search URL for general
   crawlers (a Yandex-specific block additionally disallows the bare search path,
   but that doesn't apply to us). It does NOT disallow the first page of search
   results or individual job detail pages. So this adapter never requests a `page=`
   URL - it only ever loads page 1 per query and relies on running multiple distinct
   queries for breadth, plus visits individual job detail pages (allowed) to pull
   the full description, since the search-result cards only carry a "Minimum
   qualifications" summary.
3. Job cards are found via `a[aria-label^="Learn more about "]` (a stable
   accessibility hook) rather than Google's hashed/build-generated CSS classes
   (`sMn82b`, `QJPWVe`, etc.), which are expected to churn across deploys. Within a
   card, company/location are read off the sibling text next to Material Symbols
   icon ligatures ("corporate_fare", "place") rather than more hashed classes - the
   icon ligature names are part of Google's design system and change far less often.
4. Google Careers doesn't expose a posting timestamp anywhere (list or detail page),
   so `posted_at` is always None for this source; recency/dedup relies entirely on
   the pipeline's SeenJobs table, not on this adapter's `since` filtering.

This module remains inherently more fragile than `amazon.py` since it depends on
Google's rendered DOM structure rather than a stable JSON contract - if extraction
starts silently returning fewer fields, re-run the spike in this docstring first.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import quote, urljoin

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from adapters._browser import launch_browser_context
from shared.contracts import JobPosting

logger = logging.getLogger(__name__)

_APPLICATIONS_ROOT = "https://www.google.com/about/careers/applications/"
_SEARCH_BASE = f"{_APPLICATIONS_ROOT}jobs/results/"
_DEFAULT_QUERIES = ("software engineer",)
_DEFAULT_LOCATION = "United States"
_NAV_TIMEOUT_MS = 20_000
_DESCRIPTION_MAX_CHARS = 8_000
_SALARY_RE = re.compile(r"\$[\d,]+(?:\.\d+)?\s*(?:-|to|–)\s*\$[\d,]+(?:\.\d+)?", re.IGNORECASE)

_CARD_EXTRACTION_JS = """
() => {
  const cards = [];
  const links = Array.from(document.querySelectorAll("a[aria-label^='Learn more about']"));
  for (const link of links) {
    const li = link.closest('li');
    if (!li) continue;

    const titleEl = li.querySelector('h3');
    const title = titleEl
      ? titleEl.innerText.trim()
      : (link.getAttribute('aria-label') || '').replace(/^Learn more about /, '');

    let company = null, location = null, level = null;
    for (const icon of Array.from(li.querySelectorAll('i'))) {
      const label = icon.textContent.trim();
      const sibling = icon.nextElementSibling;
      const text = sibling ? sibling.textContent.trim() : null;
      if (label === 'corporate_fare' && !company) company = text;
      if (label === 'place' && !location) location = text;
      if (label === 'bar_chart' && !level) level = text;
    }

    const headings = Array.from(li.querySelectorAll('h4'));
    const qualsHeading = headings.find(h => /minimum qualifications/i.test(h.textContent));
    let minQualifications = [];
    if (qualsHeading && qualsHeading.nextElementSibling) {
      minQualifications = Array.from(qualsHeading.nextElementSibling.querySelectorAll('li'))
        .map(x => x.innerText.trim());
    }

    cards.push({ title, company, location, level, minQualifications, href: link.getAttribute('href') });
  }
  return cards;
}
"""

# Country names as Google Careers renders them (last comma-segment of `location`) ->
# ISO 3166-1 alpha-2. Only covers common variants; falls back to the first two
# letters of the segment (handles Google's own "USA"/"UK" abbreviations tolerably).
_COUNTRY_NAME_TO_ALPHA2 = {
    "USA": "US", "US": "US", "UNITED STATES": "US", "UK": "GB", "UNITED KINGDOM": "GB",
    "CANADA": "CA", "GERMANY": "DE", "FRANCE": "FR", "IRELAND": "IE", "POLAND": "PL",
    "SWITZERLAND": "CH", "SPAIN": "ES", "ITALY": "IT", "NETHERLANDS": "NL",
    "INDIA": "IN", "SINGAPORE": "SG", "JAPAN": "JP", "SOUTH KOREA": "KR",
    "AUSTRALIA": "AU", "TAIWAN": "TW", "ISRAEL": "IL", "BRAZIL": "BR", "MEXICO": "MX",
}


class GoogleCareersAdapter:
    source_name = "google"

    def fetch_new_postings(self, since: datetime | None, filters: dict[str, Any]) -> list[JobPosting]:
        """`filters` supports:
        - `queries`: list[str] search terms (defaults to a broad software-role query).
        - `location`: str location filter passed straight through to Google's own
          `location` query param (defaults to "United States").

        `since` is accepted for Protocol compliance but not used to filter - Google
        Careers exposes no posting timestamp (see module docstring), so recency is
        left entirely to the pipeline's dedup against SeenJobs.
        """
        queries: list[str] = filters.get("queries") or list(_DEFAULT_QUERIES)
        location = filters.get("location") or _DEFAULT_LOCATION

        postings: dict[str, JobPosting] = {}
        with launch_browser_context() as context:
            page = context.new_page()
            page.set_default_navigation_timeout(_NAV_TIMEOUT_MS)

            for query in queries:
                for card in self._search(page, query, location):
                    posting = self._card_to_job_posting(page, card)
                    if posting is not None:
                        postings[posting.job_key] = posting

            page.close()

        return list(postings.values())

    def _search(self, page: Page, query: str, location: str) -> list[dict[str, Any]]:
        url = f"{_SEARCH_BASE}?q={quote(query)}&location={quote(location)}"
        try:
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_selector("a[aria-label^='Learn more about']", timeout=_NAV_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            logger.warning("google careers search returned no cards for query=%r", query)
            return []

        cards = page.evaluate(_CARD_EXTRACTION_JS)
        for card in cards:
            # Hrefs render relative to /about/careers/applications/, not to the
            # search page's own URL (which already has a `jobs/results/` suffix) -
            # joining against `url` here would double that segment.
            card["href"] = urljoin(_APPLICATIONS_ROOT, card["href"]) if card.get("href") else None
        return cards

    def _card_to_job_posting(self, page: Page, card: dict[str, Any]) -> JobPosting | None:
        href = card.get("href")
        if not href:
            return None

        job_id, canonical_url = _canonicalize(href)
        if job_id is None:
            return None

        location = card.get("location") or ""
        fallback_description = "\n".join(f"- {q}" for q in card.get("minQualifications") or [])
        description = self._fetch_description(page, canonical_url) or fallback_description
        salary_match = _SALARY_RE.search(description)

        return JobPosting(
            source=self.source_name,
            external_id=job_id,
            title=card.get("title") or "",
            company=card.get("company") or "Google",
            location=location,
            country_code=_country_code_from_location(location),
            remote_flag="remote" in location.lower(),
            url=canonical_url,
            description_text=description,
            posted_at=None,
            salary_text=salary_match.group(0) if salary_match else None,
            ats_platform="none",
            ats_board_token=None,
            ats_job_id=None,
            raw_metadata={"level": card.get("level")},
        )

    def _fetch_description(self, page: Page, url: str) -> str | None:
        try:
            page.goto(url, wait_until="domcontentloaded")
            # The "Minimum qualifications" heading isn't a consistent tag on the
            # detail page (unlike the search-card h4), so poll rendered body text
            # directly rather than waiting on a specific selector.
            page.wait_for_function(
                "() => document.body.innerText.includes('Minimum qualifications')",
                timeout=_NAV_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError:
            logger.warning("google careers detail page timed out: %s", url)
            return None

        body_text = page.inner_text("body")
        start = body_text.find("Minimum qualifications")
        if start == -1:
            return None
        return body_text[start : start + _DESCRIPTION_MAX_CHARS].strip()


def _canonicalize(href: str) -> tuple[str | None, str]:
    """Strip tracking query params and split the `{id}-{slug}` path segment."""
    path = href.split("?", 1)[0]
    slug_segment = path.rstrip("/").rsplit("/", 1)[-1]
    job_id = slug_segment.split("-", 1)[0]
    if not job_id.isdigit():
        return None, path
    return job_id, path


def _country_code_from_location(location: str) -> str:
    if not location:
        return ""
    last_segment = location.split(",")[-1].strip().upper()
    return _COUNTRY_NAME_TO_ALPHA2.get(last_segment, last_segment[:2])


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    from adapters._serialize import job_posting_to_dict

    since_raw = event.get("since")
    since = datetime.fromisoformat(since_raw) if since_raw else None
    filters = event.get("filters") or {}

    adapter = GoogleCareersAdapter()
    postings = adapter.fetch_new_postings(since, filters)
    return {"source": GoogleCareersAdapter.source_name, "postings": [job_posting_to_dict(p) for p in postings]}
