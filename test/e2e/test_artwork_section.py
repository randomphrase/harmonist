"""The Artwork section's images actually open full size (#155).

`test_web.py` can say the button carries `popovertarget`, that an element with
the matching id is in the response, and that the ids are unique. It cannot say
the browser resolves one to the other and puts it in the top layer — which is
the whole question, and a control that silently does nothing is the #40 bug
class that shipped with a green suite.

The id is derived from a content digest and truncated to twelve characters, so
"does this button reach its own image" is a real question here rather than a
formality: the folder cover appears on the incoming side of every replaced row,
and it is rendered once and pointed at from each of them.

Drives a browser via `sync_playwright()` opened per test, like every other
module here — see test_release_gone.py for why mixing in the pytest-playwright
`page` fixture kills the rest of the session.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1", reason="e2e disabled (set RUN_E2E=1)"
)
playwright_sync = pytest.importorskip("playwright.sync_api")

# The demo album seeded with `"art": "gaps"` — two tracks carrying one image, one
# carrying none, and a folder cover that differs from both. Two rows, and the
# folder cover pointed at from both of them.
ALBUM_ID = "demo-rel-dingoes"


def test_a_thumbnail_opens_the_image_full_size(demo_server: str) -> None:
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()

        page.goto(f"{demo_server}/album/{ALBUM_ID}")
        # The section is fetched after the page paints, like Tags and Tracks.
        page.wait_for_selector("#album-artwork .art-row")

        # `button` explicitly: the empty frame on a track with no art shares the
        # class (it is the same box) and opens nothing, by design.
        thumb = page.locator("#album-artwork button.art-row__art").first
        target = thumb.get_attribute("popovertarget")
        assert target, "the thumbnail names no popover to open"
        full = page.locator(f"#{target}")

        assert not full.is_visible(), "the full-size view is showing before anything was clicked"
        thumb.click()
        # Visible AND in the top layer: a popover that renders inside the
        # section's own box would be clipped by it rather than covering the page.
        full.wait_for(state="visible")
        assert full.evaluate("el => el.matches(':popover-open')")

        # Escape closes it, which is the half of this a hand-rolled overlay
        # would have had to reimplement.
        page.keyboard.press("Escape")
        full.wait_for(state="hidden")

        browser.close()


def test_every_row_reaches_its_own_image(demo_server: str) -> None:
    """Each button opens a popover that exists, and the ids are unique.

    The failure this rules out is specific: the folder cover is shown by every
    row a re-tag would write to, and emitting its popover once per row would put
    duplicate ids on the page — legal-looking markup where every button opens
    whichever copy came last.
    """
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()

        page.goto(f"{demo_server}/album/{ALBUM_ID}")
        page.wait_for_selector("#album-artwork .art-row")

        targets = [
            b.get_attribute("popovertarget")
            for b in page.locator("#album-artwork button.art-row__art").all()
        ]
        assert len(targets) >= 3, "expected both rows' images plus the incoming cover"

        for target in targets:
            assert page.locator(f"#{target}").count() == 1, f"{target} is not a single element"

        browser.close()
