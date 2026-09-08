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
    """Each button opens a popover that exists, and exactly one of them.

    Two lists are built from the album's images — the rows, and the full-size
    views `ArtworkView.distinct` emits — and this is the seam between them. A row
    drawn from an image the popover list left out is a button that opens nothing,
    which looks identical to a working one until it is pressed; two elements
    under one id is the same failure from the other direction.
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
        # The tracks' image and the folder cover — one button each. The folder
        # cover is no longer drawn a second time per row it would replace (#406).
        assert len(targets) == 2, "expected the tracks' image and the folder cover"

        for target in targets:
            assert page.locator(f"#{target}").count() == 1, f"{target} is not a single element"

        browser.close()


def test_the_archive_is_asked_without_anyone_pressing_anything(demo_server: str) -> None:
    """The Artwork section triggers its own Cover Art Archive check (#436).

    Only a browser can see this. `test_web.py` can say the response carries an
    element with `hx-get` and `hx-trigger="load"` — it said exactly that about
    the #40 button, which had stopped sending anything — and cannot say whether
    HTMX processed content that arrived inside another swap and fired the
    trigger, which is the whole question.

    The "CAA checked" row is rendered as "not yet" — the demo store is empty at
    startup — and becomes a time with nothing clicked.

    Asserted on the row's own `title`, not on the elapsed text: "just now" is
    also what the MusicBrainz row beside it says, and the transition out of "not
    yet" is too quick against a local demo server to be caught on the way past.

    Its OWN album, not the one the tests above share. The demo server is
    module-scoped, and this is the one question here whose answer depends on
    whether this album has been looked at before — an album a neighbour has
    already opened has already been asked about.
    """
    album_id = "demo-rel-barryjive"
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()

        page.goto(f"{demo_server}/album/{album_id}")
        page.wait_for_selector("#album-artwork .art-row")

        dates = page.locator(f"#album-checked-{album_id}")
        # This element exists only where an answer does: the row renders "not
        # yet" in its place until the archive has been asked (#419).
        dates.locator('dd[title^="Cover Art Archive last asked"]').wait_for(state="visible")
        assert "not yet" not in dates.inner_text()

        browser.close()
