"""Browser tests for the Activity feed's live-update behaviour.

The feed re-polls every 2s while visible. Whether that swap *replaces* the DOM
or *patches* it is invisible to the Python suite — both produce identical HTML —
but it's the whole difference between a steady list and one that flickers, drops
text selection, and slams open disclosures shut.

Opt-in: requires playwright (`pip install -e .[e2e]` + `playwright install
chromium`) and RUN_E2E=1. Run via `make e2e`.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1", reason="e2e disabled (set RUN_E2E=1)"
)
playwright_sync = pytest.importorskip("playwright.sync_api")


def test_feed_poll_patches_the_dom_instead_of_rebuilding_it(demo_server: str) -> None:
    """#91: the 2s re-poll must morph, not replace.

    Asserted by node IDENTITY — the only thing that actually distinguishes the
    two, since both produce identical HTML. Stash a reference to a live row, wait
    for a poll to land, and check the row in the DOM is still the SAME element.
    Under `hx-swap="innerHTML"` it is a freshly built node, which is the flicker.

    Deliberately not asserted via a `data-*` attribute: idiomorph SYNCS
    attributes, so one absent from the incoming HTML is stripped even when the
    node itself is preserved — that check fails on a working morph.
    """
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(demo_server)
        page.click('[data-tab="activity"]')

        row = page.locator("#activity-feed li").first
        row.wait_for(state="visible")
        page.evaluate("window.__firstRow = document.querySelector('#activity-feed li')")

        # Wait for at least one poll cycle (2s) to land.
        page.wait_for_timeout(3000)

        same_node = page.evaluate(
            "document.querySelector('#activity-feed li') === window.__firstRow"
        )
        assert same_node, "the feed rebuilt its rows instead of morphing them"

        browser.close()
