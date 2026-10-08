"""The header and the Tracks heading row fit a phone-width screen (#710).

Both rows were `flex justify-between` with no wrap, so at 375px the Sync Bandcamp
button ran ~7px off the right edge and Show identifiers ~130px — the page scrolled
sideways. Whether a row wraps is decided by layout, which only a browser does;
`test_web.py` sees the same class list either way.

Scoped to the two rows rather than the whole page: the History field list (#708)
and the per-track table behind "Show which tracks" still overflow on a phone, and
a whole-page assertion would be measuring them.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1", reason="e2e disabled (set RUN_E2E=1)"
)
playwright_sync = pytest.importorskip("playwright.sync_api")

# Has identifier columns, so the Tracks row carries Show identifiers and its
# summary — the widest thing that row holds.
ALBUM_ID = "demo-rel-rural-juror"

ROWS = ("header > div", "#album-track-view > div:first-child")

# The right edge of the furthest-reaching visible element in each row.
_RIGHT_EDGES = """(rows) => rows.map(sel => {
    const row = document.querySelector(sel);
    return Math.max(...[row, ...row.querySelectorAll('*')]
        .filter(e => e.checkVisibility())
        .map(e => e.getBoundingClientRect().right));
})"""


def _tops(page, selectors: list[str]) -> list[float]:
    tops = []
    for sel in selectors:
        box = page.locator(sel).bounding_box()
        assert box, sel
        tops.append(box["y"] + box["height"] / 2)
    return tops


def test_header_and_tracks_heading_fit_a_phone(demo_server: str) -> None:
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 375, "height": 812})
        page.goto(f"{demo_server}/album/{ALBUM_ID}")
        page.wait_for_load_state("networkidle")
        page.locator("label:has-text('Show identifiers')").wait_for(timeout=10_000)

        width = page.evaluate("document.documentElement.clientWidth")
        for sel, right in zip(ROWS, page.evaluate(_RIGHT_EDGES, list(ROWS)), strict=True):
            assert right <= width, f"{sel} reaches {right}px on a {width}px screen"

        browser.close()


def test_from_sm_up_each_row_stays_on_one_line(demo_server: str) -> None:
    """Wrapping is for phones: from `sm` up the nav sits beside the logo, and
    Show identifiers beside Edit track assignments, as before #710.

    At `sm` itself because that is where it can go wrong. A plain `flex-wrap`
    on the Tracks row wraps at 640 and 768 too — flex wraps on an item's
    max-content width, long before anything overflows — while every wider
    screen still looks right."""
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 640, "height": 900})
        page.goto(f"{demo_server}/album/{ALBUM_ID}")
        page.wait_for_load_state("networkidle")
        page.locator("label:has-text('Show identifiers')").wait_for(timeout=10_000)

        logo, nav = _tops(page, ["header a[href='/']", "header nav"])
        assert abs(logo - nav) < 2, "the nav dropped below the logo"
        edit, label = _tops(
            page,
            [
                "#album-track-view button:has-text('Edit track assignments')",
                "label:has-text('Show identifiers')",
            ],
        )
        assert abs(edit - label) < 2, "Show identifiers dropped below the heading"

        browser.close()
