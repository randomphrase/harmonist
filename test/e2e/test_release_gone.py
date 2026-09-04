"""The deleted-release banner reaches the page, out of band (#210).

Out-of-band swaps are exactly what the Python suite cannot check: it sees the
response HTML and asserts the right markup came back, but not whether htmx
placed it in the right elements — or whether those elements exist on the page at
all. A typo in an id produces a perfectly correct-looking response that lands
nowhere.

Drives a browser the same way every other module here does — `sync_playwright()`
opened per test — rather than through the pytest-playwright plugin's `page`
fixture. Mixing the two is fatal to the run: the plugin leaves an event loop
running for the rest of the session, and the next module to open its own
`sync_playwright()` dies on "Sync API inside the asyncio loop" before its first
line. That cost the three sync-popover tests, which are only guilty of sorting
after this file.
"""

from __future__ import annotations

import os

import pytest

# Opt-in like every other module here: `make check` stays browser-free.
pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1", reason="e2e disabled (set RUN_E2E=1)"
)
playwright_sync = pytest.importorskip("playwright.sync_api")


def test_the_banner_lands_and_no_retag_is_offered(demo_server: str) -> None:
    """Demo seeds one album whose release is absent from the catalogue."""
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()

        page.goto(f"{demo_server}/?tab=library")
        page.wait_for_load_state("networkidle")

        page.goto(f"{demo_server}/album/demo-rel-deleted")
        # The banner arrives with the /compare fetch, not with the page.
        banner = page.locator("text=This release is gone from MusicBrainz")
        banner.wait_for(timeout=10_000)

        assert banner.is_visible(), "OOB swap landed in #album-alert-*"

        # No control at all, rather than a disabled one. Re-tag moved into the
        # update section in #366, and that section draws it only where
        # MusicBrainz actually has the release — so on a deleted one there is
        # nothing to disable, and nothing is what the page should show.
        assert page.locator("#retag-btn-demo-rel-deleted").count() == 0

        find = page.get_by_role("button", name="Find a new release")
        assert find.is_visible()

        browser.close()


def test_a_healthy_album_gets_no_banner(demo_server: str) -> None:
    """The control — the OOB swaps must not fire for an album that is fine."""
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()

        page.goto(f"{demo_server}/album/demo-rel-rural-juror")
        page.wait_for_selector("#album-tracks table, #album-tracks", timeout=10_000)

        assert page.locator("text=This release is gone from MusicBrainz").count() == 0
        retag = page.locator("#retag-btn-demo-rel-rural-juror")
        retag.wait_for(timeout=10_000)
        assert not retag.is_disabled(), "the live one, on an album MusicBrainz still has"

        browser.close()
