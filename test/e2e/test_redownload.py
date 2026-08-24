"""The Re-download button actually issues its request (#132).

This is the #40 bug class, and the Python suite structurally cannot see it: it
renders the album page and asserts the button's markup is right, which it was in
#40 too. What it can't see is whether a click reaches htmx — and this button
carries the exact combination that ate #40's, an `hx-confirm` gating the request
plus an `hx-on::after-request` that navigates away the moment it lands.

`make template-lint` covers the mechanical half (no `onclick` beside `hx-*`).
The half left over is the confirm dialog: htmx uses `window.confirm`, which
Playwright auto-dismisses unless a handler accepts it, and a dismissed confirm
looks identical from the server's side to a button that never fired. So the
assertion is on the request itself, not on the aftermath.

Drives a browser via `sync_playwright()` opened per test, like every other module
here — see test_release_gone.py for why mixing in the pytest-playwright `page`
fixture kills the rest of the session.

Runs LAST-ish by filename and in its own module so the module-scoped
`demo_server` is its own process: this test genuinely deletes an album from the
demo library, which is not a thing to hand to a neighbouring test.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1", reason="e2e disabled (set RUN_E2E=1)"
)
playwright_sync = pytest.importorskip("playwright.sync_api")

# INCOMPLETE, linked to Bandcamp purchase 1005 — the issue's own case: an album
# that is short and whose store link means the missing tracks can still be got.
ALBUM_ID = "demo-rel-electric-mayhem"

# A SECOND linked album (COMPLETE, purchase 1003) for the dismiss test, because
# the confirm test really does delete the one above and `demo_server` is
# module-scoped. Two albums keeps the pair order-independent, which matters:
# tests here run in random order.
UNTOUCHED_ALBUM_ID = "demo-rel-rural-juror"


def test_confirming_the_dialog_posts_the_redownload(demo_server: str) -> None:
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        # Accept htmx's confirm. Without this Playwright dismisses it, no request
        # is made, and every assertion about the aftermath would fail for a
        # reason that has nothing to do with the button.
        page.on("dialog", lambda d: d.accept())

        page.goto(f"{demo_server}/album/{ALBUM_ID}")
        page.wait_for_load_state("networkidle")

        button = page.get_by_role("button", name="Re-download")
        assert button.is_visible(), "the album is linked to a purchase, so it's offered"

        with page.expect_request(f"**/library/{ALBUM_ID}/redownload") as request:
            button.click()
        assert request.value.method == "POST"

        # The album has left the Library, so the page describing it must not be
        # where the user is left standing.
        page.wait_for_url(f"{demo_server}/", timeout=10_000)
        browser.close()


def test_dismissing_the_dialog_posts_nothing(demo_server: str) -> None:
    """The control: `hx-confirm` has to be able to stop the request, or the
    confirmation is decoration on an operation that deletes files."""
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.on("dialog", lambda d: d.dismiss())

        posts: list[str] = []
        page.on(
            "request",
            lambda r: posts.append(r.url) if r.method == "POST" else None,
        )

        page.goto(f"{demo_server}/album/{UNTOUCHED_ALBUM_ID}")
        page.wait_for_load_state("networkidle")
        page.get_by_role("button", name="Re-download").click()
        page.wait_for_timeout(500)

        assert not [u for u in posts if "redownload" in u]
        assert page.url.endswith(f"/album/{UNTOUCHED_ALBUM_ID}"), "still on the album page"
        browser.close()
