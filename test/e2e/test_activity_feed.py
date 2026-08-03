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


def test_status_pill_is_not_rebuilt_when_unchanged(demo_server: str) -> None:
    """#93: the header status pill must not be re-rendered on every 1.5s poll.

    `renderStatus()` assigned innerHTML unconditionally, which destroys and
    rebuilds the children even when the markup is byte-identical — 40 rebuilds a
    minute for as long as any message shows, and `latestActionMessage` persists
    after any action. Same node-identity check as the feed test below, for the
    same reason: the rendered HTML is identical either way.
    """
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(demo_server)

        # Fire the event the action routes emit, so a pill exists to watch.
        page.evaluate(
            "document.body.dispatchEvent(new CustomEvent('harmonist-status',"
            "{detail:{verb:'Probe', details:'holding steady', level:'info'}}))"
        )
        pill = page.locator("#harmonist-status > *").first
        pill.wait_for(state="attached")
        page.evaluate(
            "window.__pill = document.querySelector('#harmonist-status').firstElementChild"
        )

        page.wait_for_timeout(3500)  # more than two 1.5s status polls

        same_node = page.evaluate(
            "document.querySelector('#harmonist-status').firstElementChild === window.__pill"
        )
        assert same_node, "the status pill was rebuilt despite unchanged content"

        browser.close()


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
