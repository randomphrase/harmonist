"""Original UPC alone drives the live sibling review and contribution flow."""

import os

import pytest
from mutagen.mp4 import MP4
from test.test_upc_contributions import MBID, UPC

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
playwright_sync = pytest.importorskip("playwright.sync_api")


def test_barcode_only_checks_releases_before_edits(barcode_contribution_server):
    server, scenario = barcode_contribution_server
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        pending = []
        page.route("**/contributions/editions", lambda route: pending.append(route))
        for visit in range(2):
            if visit:
                page.reload()
            else:
                page.goto(f"{server}/album/{MBID}")
            panel = page.locator(f"#album-contributions-{MBID}")
            playwright_sync.expect(panel).to_contain_text("Checking digital releases")
            playwright_sync.expect(panel.locator('a[href$="/edit"]')).to_have_count(0)
            playwright_sync.expect(panel.locator("p > strong")).to_have_count(0)
            page.wait_for_function(
                "() => document.querySelector('[id^=\"contribution-editions-\"].htmx-request') !== null"
            )
            assert len(pending) == visit + 1
            pending[-1].continue_()
            playwright_sync.expect(panel.locator("p > strong")).to_have_count(1)
            if scenario == "linked":
                playwright_sync.expect(panel).to_contain_text("Possible better MB match found")
                playwright_sync.expect(panel.get_by_role("listitem").first).to_contain_text(
                    "Barcode matches"
                )
                playwright_sync.expect(panel.locator('a[href$="/edit"]')).to_have_count(0)
                playwright_sync.expect(panel.get_by_role("link", name="Add Release")).to_have_count(
                    0
                )
            else:
                playwright_sync.expect(panel).to_contain_text("Barcode missing from MusicBrainz")
                playwright_sync.expect(
                    panel.get_by_role("textbox", name="Original UPC to copy")
                ).to_have_value(UPC)
                playwright_sync.expect(
                    panel.get_by_role("link", name="Add barcode on MusicBrainz")
                ).to_be_visible()
            assert len(pending) == visit + 1
        browser.close()


def test_upc_sibling_review_prioritizes_rematch(upc_contribution_server):
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
        playwright_sync.expect(panel).to_contain_text("Possible better MB match found")
        playwright_sync.expect(panel.locator("p > strong")).to_have_count(1)
        results = panel.get_by_role("region", name="Digital releases")
        suggested = results.get_by_role("listitem", name="Suggested digital release")
        playwright_sync.expect(suggested).to_contain_text("Barcode matches")
        assert len(discoveries) == 1
        playwright_sync.expect(panel.get_by_role("link", name="Add Release")).to_have_count(0)
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
        # An unbarcoded sibling stays selectable, but confirming it must check
        # for the existing source match again before offering a barcode edit.
        results.get_by_role("listitem").filter(has_text="Reissue").get_by_role(
            "button", name="Use", exact=True
        ).click()
        review.get_by_role("button", name="Confirm suggestion", exact=True).click()
        page.wait_for_url("**/album/upc-unlinked*")
        panel = page.locator("#album-contributions-upc-unlinked")
        playwright_sync.expect(panel).to_contain_text("Possible better MB match found")
        playwright_sync.expect(panel.locator("p > strong")).to_have_count(1)
        playwright_sync.expect(panel.locator('a[href$="/edit"]')).to_have_count(0)
        assert MP4(file)["----:com.apple.iTunes:UPC"] == [UPC.encode()]
        with page.expect_response(lambda r: "/compare?reread=1" in r.url) as refreshed:
            page.get_by_role(
                "button", name="Read this release from MusicBrainz again", exact=True
            ).click()
        assert refreshed.value.ok
        playwright_sync.expect(panel).to_contain_text("Possible better MB match found")
        page.goto(f"{server}/?tab=library&filter=mb-contributions")
        playwright_sync.expect(
            page.locator("#library-page").get_by_text("Test Album", exact=True)
        ).to_be_visible()
        page.goto(f"{server}/album/upc-unlinked")
        panel = page.locator("#album-contributions-upc-unlinked")
        playwright_sync.expect(panel).to_contain_text("Possible better MB match found")
        page.wait_for_load_state("networkidle")
        panel.get_by_role("listitem", name="Suggested digital release").get_by_role(
            "button", name="Use", exact=True
        ).click()
        page.get_by_role("region", name="Review suggested release").get_by_role(
            "button", name="Confirm suggestion", exact=True
        ).click()
        page.wait_for_url("**/album/upc-digital*")
        playwright_sync.expect(
            page.locator("#album-contributions-upc-digital section")
        ).to_have_count(0)
        assert MP4(file)["----:com.apple.iTunes:UPC"] == [UPC.encode()]
        browser.close()
