"""Shared Playwright browser-session helper for adapters that scrape client-rendered
pages (Google Careers, Wellfound).

Not part of `src/shared/` - that module is frozen and owned by `main`. This is an
internal convenience shared only between adapters in this directory, so it carries
none of the cross-workstream contract weight `shared/contracts.py` does.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from playwright.sync_api import BrowserContext, Route, sync_playwright

_BLOCKED_RESOURCE_TYPES = {"image", "stylesheet", "font", "media"}

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def _block_heavy_resources(route: Route) -> None:
    if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


@contextmanager
def launch_browser_context(user_agent: str = DEFAULT_USER_AGENT) -> Iterator[BrowserContext]:
    """Launch one Chromium instance + context for the life of a batch of page loads.

    Blocks image/CSS/font/media requests so each navigation stays fast and cheap.
    Caller opens/closes individual pages within the yielded context and should reuse
    this one context for every page load in a Lambda invocation rather than launching
    a fresh browser per job.
    """
    with sync_playwright() as playwright:
        # Lambda's execution environment can't set up Chromium's usual sandbox
        # (no unprivileged user namespaces) and has no GPU - --no-sandbox and
        # --disable-gpu are required for Chromium to launch at all there;
        # --single-process avoids assumptions about /dev/shm and multi-process
        # IPC that don't hold up in that environment either.
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--single-process"],
        )
        try:
            context = browser.new_context(user_agent=user_agent)
            context.route("**/*", _block_heavy_resources)
            try:
                yield context
            finally:
                context.close()
        finally:
            browser.close()
