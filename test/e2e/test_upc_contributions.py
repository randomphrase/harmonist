"""Original UPC alone drives the live sibling review and contribution flow."""

import os

import pytest
from mutagen.mp4 import MP4
from test.test_upc_contributions import MBID, UPC

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
playwright_sync = pytest.importorskip("playwright.sync_api")


def test_upc_sibling_review_and_missing_barcode(upc_contribution_server):
    server, root = upc_contribution_server
    file = root / "Download" / "01.m4a"
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        discoveries = []
        page.on(
            "request",
            lambda r: (
                discoveries.append(r.url) if r.url.endswith("/contributions/editions") else None
            ),
        )
        page.goto(f"{server}/album/{MBID}")
        panel = page.locator(f"#album-contributions-{MBID}")
        playwright_sync.expect(panel).to_contain_text("Possible media mismatch")
        playwright_sync.expect(
            panel.get_by_role("textbox", name="Original UPC to copy")
        ).to_have_value(UPC)
        results = panel.get_by_role("region", name="Digital editions")
        suggested = results.get_by_role("row", name="Suggested digital release")
        playwright_sync.expect(suggested).to_contain_text("Same UPC")
        assert len(discoveries) == 1
        assert "gtin=" + UPC in panel.get_by_role("link", name="Add Release").get_attribute("href")
        before = file.read_bytes()
        suggested.get_by_role("button", name="Use", exact=True).click()
        review = page.get_by_role("region", name="Review suggested release")
        playwright_sync.expect(
            review.get_by_role("button", name="Confirm suggestion", exact=True)
        ).to_be_enabled()
        playwright_sync.expect(panel).to_be_hidden()
        assert file.read_bytes() == before
        review.get_by_role("button", name="Cancel", exact=True).click()
        playwright_sync.expect(panel).to_be_visible()
        # MB's unbarcoded sibling is still selectable; its UPC contribution
        # becomes actionable after confirmation, without changing source tags.
        results.locator("tbody tr").filter(has_text="Reissue").get_by_role(
            "button", name="Use", exact=True
        ).click()
        review.get_by_role("button", name="Confirm suggestion", exact=True).click()
        page.wait_for_url("**/album/upc-unlinked*")
        panel = page.locator("#album-contributions-upc-unlinked")
        playwright_sync.expect(panel).to_contain_text("Barcode missing from MusicBrainz")
        playwright_sync.expect(
            panel.get_by_role("textbox", name="Original UPC to copy")
        ).to_have_value(UPC)
        assert (
            panel.get_by_role("link", name="Add barcode on MusicBrainz").get_attribute("href")
            == "https://musicbrainz.org/release/upc-unlinked/edit"
        )
        assert MP4(file)["----:com.apple.iTunes:UPC"] == [UPC.encode()]
        with page.expect_response(lambda r: "/compare?reread=1" in r.url) as refreshed:
            page.get_by_role(
                "button", name="Read this release from MusicBrainz again", exact=True
            ).click()
        assert refreshed.value.ok
        playwright_sync.expect(panel).to_contain_text("Barcode missing from MusicBrainz")
        page.goto(f"{server}/?tab=library&filter=mb-contributions")
        playwright_sync.expect(
            page.locator("#library-page").get_by_text("Test Album", exact=True)
        ).to_be_visible()
        browser.close()
