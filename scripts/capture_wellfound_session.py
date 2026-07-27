"""One-time (and periodic re-run, once the session expires) manual capture of
an authenticated Wellfound session for WellfoundAdapter.

Not a Lambda, not automated - opens a real, visible browser window on this
machine. You log in yourself, by hand, exactly like any other visit to
Wellfound; this script only saves the resulting session (cookies +
localStorage) once you confirm you're logged in. Nothing here ever sees or
transmits your password - only the session state Wellfound itself issues
after a normal login.

That session is a live credential, not a static string - treat the output
file the same as a password (it's git-ignored via *.local; never commit it)
and expect to re-run this every so often once the session expires.

Usage:
    python scripts/capture_wellfound_session.py
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "wellfound_storage_state.local.json"


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://wellfound.com/login")

        print("A real browser window just opened. Log in to Wellfound yourself, normally.")
        print("Once you're fully logged in and see your dashboard/feed, come back here and press Enter.")
        input()

        context.storage_state(path=str(OUTPUT_PATH))
        browser.close()

    print(f"Saved session to {OUTPUT_PATH}")
    print("This file is a live credential - don't share it, don't commit it.")
    print("Next: tell Claude it's ready, so it can load it into Secrets Manager and delete the local copy.")


if __name__ == "__main__":
    main()
