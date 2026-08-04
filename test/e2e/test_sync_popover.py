"""Browser test for the sync popover's binding to its button (#110).

Invisible to the Python suite by construction: the popover is revealed by
`group-hover:` and suppressed by a `:has()` rule, so whether it opens is a
question about CSS cascade under a real hover — pytest can only assert that the
two hooks exist. The bug this covers shipped precisely because the markup and
the gating JS were each individually fine.

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


def test_popover_opens_on_hover_when_sync_is_live(demo_server: str) -> None:
    """The control: hovering an enabled Sync button reveals the popover. Without
    this, the suppression test below would pass against a popover that never
    opens at all."""
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(demo_server)

        button = page.locator("#sync-button")
        button.wait_for(state="visible")
        assert not button.is_disabled()

        popover = page.locator("#sync-popover")
        assert not popover.is_visible()
        button.hover()
        popover.wait_for(state="visible", timeout=3000)
        # The submit inside is the second sync trigger this is all about.
        assert page.locator('#sync-popover button[type="submit"]').is_visible()

        browser.close()


def test_popover_cannot_open_over_a_disabled_sync_button(demo_server: str) -> None:
    """A disabled Sync button must take the popover with it. `disabled` does not
    suppress :hover on the wrapper, so before #110 the popover opened over the
    greyed-out button and its submit started a sync anyway."""
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(demo_server)

        button = page.locator("#sync-button")
        button.wait_for(state="visible")
        # Stop the /status poll FIRST. It re-runs every second and re-enables
        # the button whenever nothing is busy, so without this the assertion
        # below is a race against the next tick — it would pass or fail
        # depending on timing, which is exactly the flake #52 cost us.
        page.evaluate("for (let i = 1; i < 10000; i++) clearInterval(i)")
        # Disable it the same way every real reason does — the attribute. The
        # poll JS sets exactly this during a sync, reconcile or cold-start scan,
        # and /settings renders it server-side.
        button.evaluate("b => b.disabled = true")

        button.hover(force=True)
        page.wait_for_timeout(500)  # let any hover transition settle
        assert button.is_disabled(), "poll re-enabled the button; the assertion below is moot"
        assert not page.locator("#sync-popover").is_visible()

        browser.close()


def test_settings_page_ships_a_disabled_button_and_a_shut_popover(demo_server: str) -> None:
    """End to end on the real page: /settings renders the button disabled
    server-side (#108) and the popover stays shut on hover (#110) — no JS
    involved, since the status poll doesn't run on this page."""
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(f"{demo_server}/settings")

        button = page.locator("#sync-button")
        button.wait_for(state="visible")
        assert button.is_disabled()

        button.hover(force=True)  # force: a disabled button swallows real hovers
        page.wait_for_timeout(500)
        assert not page.locator("#sync-popover").is_visible()

        browser.close()
