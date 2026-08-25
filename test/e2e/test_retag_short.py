"""The "MusicBrainz has grown" offer lands on the page, and its button fires (#252).

Two things the Python suite structurally cannot check, both of which have bitten
this repo before:

- The offer arrives as an **out-of-band swap**. `test_web.py` asserts the right
  markup came back in the response; it cannot say whether htmx put it anywhere,
  or whether `#album-alert-<id>` exists on the album page at all. A typo in the
  id produces a perfectly correct-looking response that lands nowhere (#210).
- The offered button carries `hx-vals`. Whether `accept_short` actually reaches
  the server is a question about the browser, not about the template — and a
  control that silently does nothing is the #40 bug class exactly.

So the second test asserts on the POST body, then on the outcome.

Drives a browser via `sync_playwright()` opened per test, like every other module
here — see test_release_gone.py for why mixing in the pytest-playwright `page`
fixture kills the rest of the session.

Own module because the second test really does re-tag the demo album, which is
not a thing to hand to a neighbouring test: `demo_server` is module-scoped, so
that mutation stays in here.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1", reason="e2e disabled (set RUN_E2E=1)"
)
playwright_sync = pytest.importorskip("playwright.sync_api")

# COMPLETE by its own files (2 of 2), while the demo catalogue's release lists 4
# — the album the demo seeds for this state.
ALBUM_ID = "demo-rel-wonders"

# A COMPLETE album whose files and the catalogue agree, for the control.
AGREEING_ALBUM_ID = "demo-rel-rural-juror"


def test_a_re_tag_that_cannot_fit_offers_the_way_out(demo_server: str) -> None:
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()

        page.goto(f"{demo_server}/album/{AGREEING_ALBUM_ID}")
        page.wait_for_load_state("networkidle")
        page.get_by_role("button", name="Re-tag from MB").click()
        # The control first: an album that fits gets no alert at all, so the
        # assertion below is about this state rather than about the slot always
        # being full.
        page.wait_for_timeout(1500)
        assert page.locator("text=MusicBrainz now lists").count() == 0

        page.goto(f"{demo_server}/album/{ALBUM_ID}")
        page.wait_for_load_state("networkidle")
        page.get_by_role("button", name="Re-tag from MB").click()

        offer = page.locator("text=MusicBrainz now lists 4 tracks — you have 2")
        offer.wait_for(timeout=10_000)
        assert offer.is_visible(), "the OOB swap landed in #album-alert-*"
        assert page.get_by_role("button", name="Re-tag as incomplete").is_visible()

        browser.close()


def test_the_offered_button_sends_the_decision(demo_server: str) -> None:
    """`hx-vals` is the whole mechanism here: without `accept_short` on the wire
    the second press hits the same guard as the first and nothing ever changes."""
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()

        page.goto(f"{demo_server}/album/{ALBUM_ID}")
        page.wait_for_load_state("networkidle")
        page.get_by_role("button", name="Re-tag from MB").click()
        page.locator("text=MusicBrainz now lists 4 tracks — you have 2").wait_for(timeout=10_000)

        with page.expect_request(f"**/retag/{ALBUM_ID}") as request:
            page.get_by_role("button", name="Re-tag as incomplete").click()
        assert request.value.method == "POST"
        assert "accept_short=true" in (request.value.post_data or "")

        # The re-tag reloads the page (album.html's `album-retagged` listener),
        # and the album now states the shortfall it just accepted.
        page.wait_for_selector("text=2 of 4 tracks on disk", timeout=10_000)
        browser.close()
