"""The Re-download button actually issues its request (#132).

This is the #40 bug class, and the Python suite structurally cannot see it: it
renders the album page and asserts the button's markup is right, which it was in
#40 too. What it can't see is whether a click reaches htmx — and this button
carries the exact combination that ate #40's, an `hx-confirm` gating the request
plus an `hx-on::after-request` that redraws the page the moment it lands.

Nor can it see what the page does afterwards (#1003): it stays on the album
while the album is off disk, and becomes the album's page again by itself when
the replacement lands, which is a listener, a fetch and a reload.

`make template-lint` covers the mechanical half (no `onclick` beside `hx-*`).
The half left over is the confirm dialog: htmx uses `window.confirm`, which
Playwright auto-dismisses unless a handler accepts it, and a dismissed confirm
looks identical from the server's side to a button that never fired. So the
assertion is on the request itself, not on the aftermath.

On a server of its own (`redownload_server`): this test genuinely deletes an
album from the demo library, which is not a thing to hand to a neighbouring
test, and it holds the sync to see the page in between.
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
# the confirm test really does delete the one above and `redownload_server` is
# module-scoped. Two albums keeps the pair order-independent, which matters:
# tests here run in random order.
UNTOUCHED_ALBUM_ID = "demo-rel-rural-juror"

# The CSRF middleware wants this on every write, the test server's own included.
HTMX = {"HX-Request": "true"}


def test_confirming_the_dialog_redownloads_and_stays_on_the_album(
    browser_factory, redownload_server: str
) -> None:
    base = redownload_server
    browser = browser_factory("chromium")
    page = browser.new_page()
    # Accept htmx's confirm. Without this Playwright dismisses it, no request
    # is made, and every assertion about the aftermath would fail for a
    # reason that has nothing to do with the button.
    page.on("dialog", lambda d: d.accept())

    page.goto(f"{base}/album/{ALBUM_ID}")
    page.wait_for_load_state("networkidle")

    button = page.get_by_role("button", name="Re-download")
    assert button.is_visible(), "the album is linked to a purchase, so it's offered"

    # Held, or the demo's sync has the album back before the page can show the
    # wait for it.
    assert page.request.post(f"{base}/test/sync/hold", headers=HTMX).ok
    with page.expect_request(f"**/library/{ALBUM_ID}/redownload") as request:
        button.click()
    assert request.value.method == "POST"

    # The album is off disk, and its own page is where the reader waits for it
    # (#1003): it says so, and names the zip that is the way back.
    waiting = page.locator("#redownload-section")
    playwright_sync.expect(waiting).to_contain_text("Re-downloading")
    playwright_sync.expect(waiting).to_contain_text("(archived ")
    assert page.url == f"{base}/album/{ALBUM_ID}"

    # ...and it is the album's page again once the replacement is in the
    # library, with nobody having pressed anything.
    assert page.request.post(f"{base}/test/sync/release", headers=HTMX).ok
    playwright_sync.expect(page.get_by_role("button", name="Re-download")).to_be_visible(
        timeout=15_000
    )
    playwright_sync.expect(page.locator(".album-history")).to_contain_text(
        "Re-downloaded and tagged as the same MusicBrainz release"
    )
    assert page.url == f"{base}/album/{ALBUM_ID}"
    page.context.close()


def test_dismissing_the_dialog_posts_nothing(browser_factory, redownload_server: str) -> None:
    """The control: `hx-confirm` has to be able to stop the request, or the
    confirmation is decoration on an operation that deletes files."""
    browser = browser_factory("chromium")
    page = browser.new_page()
    page.on("dialog", lambda d: d.dismiss())

    posts: list[str] = []
    page.on(
        "request",
        lambda r: posts.append(r.url) if r.method == "POST" else None,
    )

    page.goto(f"{redownload_server}/album/{UNTOUCHED_ALBUM_ID}")
    page.wait_for_load_state("networkidle")
    with page.expect_event("dialog") as confirmation:
        page.get_by_role("button", name="Re-download").click()
    assert confirmation.value.type == "confirm"
    # confirm() blocks the click handler until dismissal. Its return decides
    # synchronously whether HTMX sends the write; the accepted case above is
    # the positive control.

    assert not [u for u in posts if "redownload" in u]
    assert page.url.endswith(f"/album/{UNTOUCHED_ALBUM_ID}"), "still on the album page"
    page.context.close()
