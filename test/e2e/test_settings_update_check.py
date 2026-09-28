"""Browser test for the background update check's status line on Settings (#623).

The line polls its own route from inside the preferences form, and whether the
timer fires, what it requests and what it swaps are questions only a browser
answers — the Python suite sees a correct-looking attribute either way. The
request set is asserted whole, so a poll that also submitted the surrounding form
would fail rather than render as a page that looks fine.

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


def test_turning_the_check_on_shows_its_status_and_keeps_it_current(demo_server: str) -> None:
    """Choose a level and save: the status line appears, and each minute it asks
    its route again and replaces itself — one GET, and nothing else."""
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.clock.install()
        page.goto(f"{demo_server}/settings")

        line = page.locator("#update-check-status")
        playwright_sync.expect(line).to_have_count(0)  # the demo ships the check off

        page.select_option('select[name="gardener_level"]', "review")
        page.locator('button[type="submit"]:has-text("Save settings")').click()
        page.wait_for_selector("text=Settings saved")
        playwright_sync.expect(line).to_contain_text("albums checked in the last week")

        requests: list[tuple[str, str]] = []
        page.on("request", lambda r: requests.append((r.method, r.url)))
        line.evaluate("el => el.dataset.stale = 'true'")
        with page.expect_response(lambda r: r.url.endswith("/settings/update-check")):
            page.clock.fast_forward("01:05")
        playwright_sync.expect(line).not_to_have_attribute("data-stale", "true")
        playwright_sync.expect(line).to_contain_text("albums checked in the last week")
        assert requests == [("GET", f"{demo_server}/settings/update-check")]

        browser.close()
