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

        # Every button that SHOWS an image. The archive's "load" frame shares the
        # class and opens nothing by design — it fetches — so it names no target.
        targets = [
            t
            for b in page.locator("#album-artwork button.art-row__art").all()
            if (t := b.get_attribute("popovertarget"))
        ]
        # Two images on this album — the tracks' and the folder cover's — however
        # many places each is drawn: the tracks' image is also what fills the
        # gap, on the After Apply side. Counting buttons rather than images went
        # stale the moment an image could appear twice.
        assert len(set(targets)) == 2, "expected the tracks' image and the folder cover"

        for target in set(targets):
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


def test_the_archives_candidate_is_one_row_on_a_narrow_window(demo_server: str) -> None:
    """The archive's frame and the facts beside it stay one row below 56rem
    (#577), and the picker sits BENEATH the row it is for (#659).

    Only a browser can see either, and #577 is why it is worth checking: the
    markup was right and the stylesheet said the right thing, but a media query
    adds no specificity, so the unconditional rule won on source order at every
    width and the frame landed in an implicit third column away from its facts.
    Asserted geometrically, because a defect like that is entirely in where the
    boxes land: every class involved is correct either way.
    """
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        # Narrower than the 56rem the stylesheet collapses at, at the default
        # 16px root — 760px is comfortably inside it.
        page = browser.new_page(viewport={"width": 760, "height": 1400})

        page.goto(f"{demo_server}/album/{ALBUM_ID}")
        # The picker arrives with the out-of-band check, not with the section,
        # so this waits for the answer rather than for the section.
        page.wait_for_selector("#album-artwork .art-pick__picker")

        block = page.locator("#album-artwork .art-pick__candidate")
        # Whichever of the three states the frame is in: a button to fetch the
        # picture, a placeholder, or the picture itself.
        frame = block.locator(".art-row__art").first
        facts = block.locator(".art-row__facts").first

        f = frame.bounding_box()
        t = facts.bounding_box()
        assert f and t

        # Beside, not above: the frame ends before its facts begin…
        assert f["x"] + f["width"] <= t["x"], "the archive's facts are not beside its frame"
        # …and the two overlap vertically, which is what "one row" means.
        assert min(f["y"] + f["height"], t["y"] + t["height"]) > max(f["y"], t["y"]), (
            "the archive's frame and its facts are on separate rows"
        )
        # And the picker is under the checked row — the first — not above it
        # and not out in a column the one-column grid has no room for.
        checked = page.locator("#album-artwork .art-row").first.bounding_box()
        p = page.locator("#album-artwork .art-pick__picker").bounding_box()
        assert checked and p
        assert p["y"] >= checked["y"] + checked["height"] - 1, "the picker is not below its row"
        assert abs(p["x"] - checked["x"]) < 32, "the picker is not under its row"

        browser.close()


def test_choosing_an_image_does_not_move_the_table(reset_demo_server: str) -> None:
    """Pressing Use re-renders the section, and the table must stay where it
    was under the pointer (#659).

    It moved down by the section's spacing: the page's first render arrives
    inside /compare's out-of-band wrapper, which stays in the page, while a
    choice swaps the section's markup in directly — and `#album-artwork`'s
    `space-y-3` spaced the wrapper in the one case and the heading in the
    other. Only a browser lays either out, and only the page's own first render
    takes the /compare path, so this starts from the album page itself.
    """
    settled = "() => !document.querySelector('.htmx-request, .htmx-settling')"
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1400})
        page.goto(f"{reset_demo_server}/album/{ALBUM_ID}")
        page.wait_for_selector("#album-artwork .art-pick__picker")
        page.wait_for_function(settled)

        def table() -> float:
            return page.evaluate(
                """() => {
                const section = document.getElementById('album-artwork');
                const head = section.querySelector('.art-rows__head');
                return head.getBoundingClientRect().top - section.getBoundingClientRect().top;
            }"""
            )

        load = page.locator('#album-artwork button[hx-post$="/artwork/load-archive"]')
        if load.count():
            load.click()
            page.wait_for_function(settled)
        # …and open the page again: the archive has been asked and its picture
        # loaded, so nothing re-renders the section before the press, and the
        # layout measured is the first render's — /compare's, which is the one
        # the user is looking at when they press Use.
        page.goto(f"{reset_demo_server}/album/{ALBUM_ID}")
        page.wait_for_selector("#album-artwork button.art-pick__use", state="attached")
        page.wait_for_function(settled)
        # Whichever row the archive's image can still go to.
        use = page.locator("#album-artwork button.art-pick__use:visible")
        rows = page.locator("#album-artwork .art-row")
        for index in range(rows.count()):
            rows.nth(index).locator(".art-row__select").click()
            if use.count():
                break
        before = table()
        with page.expect_response(lambda r: "pick=archive" in r.url):
            use.click()
        page.wait_for_function(settled)

        assert table() == before, "the table moved when the section re-rendered"
        browser.close()


def test_the_picker_moves_to_the_row_that_is_checked(demo_server: str) -> None:
    """Checking a row puts the picker beside it (#659), with no request: the
    rows and the picker share one grid, and a rule the section generates per
    row puts the picker on the checked row's line. Nothing in the Python
    suite can see a grid line, and a rule scoped to the wrong id, or a
    `--art-pick-row` that nothing reads, renders markup that looks perfect."""
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1400})

        page.goto(f"{demo_server}/album/{ALBUM_ID}")
        page.wait_for_selector("#album-artwork .art-pick__picker")
        rows = page.locator("#album-artwork .art-row")
        assert rows.count() >= 2, "the module's album needs two rows to move between"
        picker = page.locator("#album-artwork .art-pick__picker")
        page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")
        sent: list[str] = []
        page.on("request", lambda r: sent.append(r.url))

        def level_with(index: int) -> None:
            box, at = rows.nth(index).bounding_box(), picker.bounding_box()
            assert box and at
            # Its top a margin below the row's, not a line further down.
            assert abs(at["y"] - box["y"]) < 16, f"the picker is not level with row {index}"
            assert at["x"] >= box["x"] + box["width"] - 1, "the picker is not beside the row"

        # Clicking a row — on its name, which is where a reader would — selects
        # it; the radio itself is drawn by the row, not shown.
        rows.nth(1).locator(".art-row__select").click()
        level_with(1)
        # Measured while the picker is somewhere else…
        alone = rows.nth(1).bounding_box()["y"]
        rows.nth(0).locator(".art-row__select").click()
        level_with(0)
        # …because hanging down over the empty column below it, the picker never
        # stretches the grid line it sits on: a panel taller than its row would
        # otherwise push the next row down and leave a gap under this one.
        assert rows.nth(1).bounding_box()["y"] == alone, "the picker pushed the next row down"

        # …and the quiet ↓ and ↑ beside it step through the rows the same way.
        page.get_by_role("button", name="Next row").click()
        level_with(1)
        page.get_by_role("button", name="Previous row").click()
        level_with(0)

        # Moving the picker is the stylesheet's: selecting a row asks nothing.
        assert not [url for url in sent if "/artwork" in url]
        browser.close()
