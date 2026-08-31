"""Browser test for the background-update-check control on Settings (#312).

The #40 bug class: **Check now** is an `hx-post` button sitting *inside* the
preferences form, and whether a click on it produces a request is a question
only a browser answers. The Python suite sees a correct-looking tag either way —
that is exactly how #40 shipped at 91% coverage.

The click's whole request set is asserted, not just the one that should be
there. A `<button>` inside a form submits it by default; htmx claims the click
and prevents that, and `type="button"` says so a second time, but neither is
visible from the markup and a save going out alongside the check would render
as a page that looks entirely successful.

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


def test_turning_the_check_on_enables_check_now_and_it_posts(demo_server: str) -> None:
    """The flow end to end: choose a level, save, press Check now.

    The button starts disabled (the demo ships `off`), the save re-renders the
    page with it live, and the press produces exactly one POST — the check.
    """
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(f"{demo_server}/settings")

        button = page.locator("#update-check-now")
        button.wait_for(state="visible")
        assert button.is_disabled(), "the demo ships the check off; the button must be inert"

        page.select_option('select[name="gardener_level"]', "review")
        page.locator('button[type="submit"]:has-text("Save settings")').click()
        page.wait_for_selector("text=Settings saved")
        assert not button.is_disabled()

        posts: list[str] = []
        page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
        button.click()
        page.wait_for_timeout(1000)

        assert posts == [f"{demo_server}/settings/update-check"]

        browser.close()
