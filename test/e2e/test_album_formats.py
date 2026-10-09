"""The format anchor, warning controls and assembled header work in a browser."""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1", reason="e2e disabled (set RUN_E2E=1)"
)
playwright_sync = pytest.importorskip("playwright.sync_api")
ALBUM_ID = "demo-rel-electric-mayhem"


def test_format_link_locates_inline_read_only_track_values(album_formats_server):
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1100})
        page.goto(f"{album_formats_server}/album/{ALBUM_ID}")
        table = page.locator("#album-track-view .track-diff")
        playwright_sync.expect(
            table.get_by_role("columnheader", name="Format", exact=True)
        ).to_be_visible()
        header = page.locator("#album-identity")
        warning = header.locator(".album-warning")
        facts = header.locator(".album-identity__fields")
        assert (
            facts.bounding_box()["y"] + facts.bounding_box()["height"]
            <= warning.bounding_box()["y"]
        )
        actions = header.locator(f"#album-actions-{ALBUM_ID}")
        assert (
            warning.bounding_box()["y"] + warning.bounding_box()["height"]
            <= actions.bounding_box()["y"]
        )
        playwright_sync.expect(
            actions.get_by_role("button", name="Forget", exact=True)
        ).to_be_visible()
        playwright_sync.expect(
            actions.get_by_role("button", name="Re-download from Bandcamp", exact=False)
        ).to_be_visible()
        header.get_by_role("link", name="1 track differs ↓", exact=True).click()
        assert page.url.endswith("#album-tracks")
        # The sticky navigation must not cover the anchor's heading.
        assert page.locator("#album-tracks h2").bounding_box()["y"] >= 60
        cell = table.get_by_role("cell", name="FLAC · 48 kHz · 24 bit", exact=True)
        playwright_sync.expect(cell).to_be_visible()
        # Rendering, not class names: format shouldn't create a second line on
        # an otherwise compact desktop row.
        assert cell.evaluate("""el => {
            const range = document.createRange(); range.selectNodeContents(el);
            return range.getBoundingClientRect().height <= parseFloat(getComputedStyle(el).lineHeight) + 1;
        }""")
        playwright_sync.expect(cell.locator("..")).to_contain_text("Movin' Right Along")
        browser.close()


def test_muting_removes_only_the_library_completeness_badge(album_formats_server, htmx_ready):
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(f"{album_formats_server}/album/{ALBUM_ID}")
        check = page.get_by_role("checkbox", name="Don't warn me about this", exact=True)
        with page.expect_response(lambda r: r.url.endswith("/tracks-unavailable")):
            htmx_ready(check).check()
        badge = page.locator(f"#album-completeness-{ALBUM_ID} .album-warning__badge--muted")
        playwright_sync.expect(badge).to_have_text("3 of 4 tracks on disk")
        assert badge.bounding_box()["x"] + badge.bounding_box()["width"] <= 390
        page.goto(f"{album_formats_server}/?tab=library")
        tile = page.locator(f'a[href^="/album/{ALBUM_ID}"]')
        playwright_sync.expect(tile).to_be_visible()
        playwright_sync.expect(tile.locator(".album-warning__badge")).to_have_count(0)
        tile.click()
        playwright_sync.expect(check).to_be_checked()
        with page.expect_response(lambda r: r.url.endswith("/tracks-unavailable")):
            htmx_ready(check).uncheck()
        playwright_sync.expect(
            page.locator(f"#album-completeness-{ALBUM_ID} .album-warning__badge--muted")
        ).to_have_count(0)
        page.goto(f"{album_formats_server}/?tab=library")
        playwright_sync.expect(tile.locator(".album-warning__badge")).to_have_text("3 of 4")
        browser.close()
