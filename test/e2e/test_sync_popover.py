"""Browser tests for the sync popover: its binding to the button (#110) and
which fields a sync actually carries (#399).

Invisible to the Python suite by construction: the popover is revealed by
`group-hover:` and suppressed by a `:has()` rule, so whether it opens is a
question about CSS cascade under a real hover — and which inputs `hx-include`
collects is a question only a browser answers. pytest can assert that the hooks
exist, nothing more. The bug this covers shipped precisely because the markup
and the gating JS were each individually fine.

Opt-in: requires playwright (`pip install -e .[e2e]` + `playwright install
chromium`) and RUN_E2E=1. Run via `make e2e`.
"""

from __future__ import annotations

import os
from typing import Any

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
        # The option the button carries — the popover's whole content since #399.
        assert page.locator("#sync-link-only").is_visible()

        browser.close()


def _sync_post_body(page: Any, *, open_popover: bool, toggle: bool) -> str:
    """Click Sync (optionally opening the popover and toggling Link-only first)
    and return the body POSTed to /sync."""
    bodies: list[str] = []
    page.on(
        "request",
        lambda r: bodies.append(r.post_data or "") if r.method == "POST" else None,
    )
    button = page.locator("#sync-button")
    button.wait_for(state="visible")
    if open_popover:
        button.hover()
        page.locator("#sync-popover").wait_for(state="visible", timeout=3000)
        if toggle:
            page.locator("#sync-link-only").click()
    button.click()
    page.wait_for_timeout(800)
    assert bodies, "Sync posted nothing"
    return bodies[-1]


def test_untouched_popover_leaves_link_only_to_the_server(demo_server: str) -> None:
    """Adoption safety (#399): since the popover lost its own submit button, the
    Sync button carries its inputs — so a user who never opened it must still get
    the server's fresh-scan auto-detect, not the checkbox's derived guess.

    `from_popover` is what makes a choice explicit, and it stays disabled until
    the checkbox is actually touched. Without that gate every sync would post an
    explicit override, and a stale-unchecked box would turn an adoption sync into
    a full one — re-downloading a library already on disk. Invisible to pytest:
    which fields hx-include collects is a browser question.
    """
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(demo_server)

        assert "from_popover" not in _sync_post_body(page, open_popover=False, toggle=False)

        browser.close()


def test_touching_link_only_makes_the_choice_explicit(demo_server: str) -> None:
    """The other half: once the user toggles Link-only, the sync must carry
    `from_popover` so the server takes the choice instead of auto-detecting."""
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(demo_server)

        assert "from_popover=true" in _sync_post_body(page, open_popover=True, toggle=True)

        browser.close()


def test_popover_cannot_open_over_a_disabled_sync_button(demo_server: str) -> None:
    """A disabled Sync button must take the popover with it. `disabled` does not
    suppress :hover on the wrapper, so before #110 the popover opened over the
    greyed-out button and its own submit started a sync anyway. That submit is
    gone (#399), but the rule still earns its keep: options floating over a
    control that cannot run are an offer Harmonist can't honour."""
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
