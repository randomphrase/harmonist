"""The "read again" control on the Tags panel actually fires (#127).

This is the #40 bug class, and the Python suite structurally cannot see it: it
asserts the right `hx-get` came back in the markup, which was also true of the
buttons in #40 that silently did nothing. Whether a click produces a *request*,
and whether the response lands in the panel rather than nowhere, is only
observable in a browser.

The stakes are specific here. The control is the escape hatch out of a stale
cached comparison (review-gate item 5). A dead one leaves the user with no way
to force a fresh read short of a re-tag — and it would look completely fine.
"""

from __future__ import annotations

import os

import pytest

# Opt-in like every other module here: `make check` stays browser-free.
pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1", reason="e2e disabled (set RUN_E2E=1)"
)
playwright_sync = pytest.importorskip("playwright.sync_api")

ALBUM = "demo-rel-dingoes"


def test_read_again_issues_a_compare_request_and_refills_the_panel(demo_server: str) -> None:
    """Click it and a `reread=1` request must actually go out, then land."""
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()

        requests: list[str] = []
        page.on("request", lambda r: requests.append(r.url))

        page.goto(f"{demo_server}/album/{ALBUM}")
        # The control arrives with the /compare fetch, not with the page.
        button = page.get_by_role("button", name="Read this release from MusicBrainz again")
        button.wait_for(timeout=10_000)

        assert not [u for u in requests if "reread=1" in u], "nothing forced yet"

        with page.expect_response(lambda r: "reread=1" in r.url, timeout=10_000) as got:
            button.click()
        assert got.value.ok

        # ...and the response was SWAPPED IN, not merely received: the panel is
        # still a comparison rather than the "Checking tags…" placeholder or an
        # empty box, which is what a mis-targeted swap would leave behind.
        page.wait_for_selector(f"#compare-{ALBUM} .mb-note", timeout=10_000)
        assert page.locator(f"#compare-{ALBUM} .mb-note__when").is_visible()

        browser.close()


def test_the_panel_reports_when_it_last_read_musicbrainz(demo_server: str) -> None:
    """The other half of the escape hatch: the user has to be able to SEE that
    what they are looking at may be old, or they never think to force a read."""
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()

        page.goto(f"{demo_server}/album/{ALBUM}")
        when = page.locator(f"#compare-{ALBUM} .mb-note__when")
        when.wait_for(timeout=10_000)

        assert "read" in (when.text_content() or "")

        browser.close()
