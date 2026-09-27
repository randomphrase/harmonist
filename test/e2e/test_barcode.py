"""The barcode picker must survive the album page's after-request handler."""

import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
playwright_sync = pytest.importorskip("playwright.sync_api")


def test_barcode_choices_open_review_without_tagging(barcode_server):
    server, root = barcode_server
    from harmonist import scanner, sidecar

    album = next(a for a in scanner.scan(root) if a.title == "Frequencies")
    file = album.path / "01 LFO.m4a"
    before = file.read_bytes()
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(f"{server}/album/{album.id}")
        # Make the interval between insertion and HTMX initialization visible:
        # a fast click must not submit the picker as a native GET navigation.
        page.evaluate("htmx.config.defaultSettleDelay = 1000")
        with page.expect_response(lambda r: r.url.endswith("/barcode")):
            page.get_by_role("button", name="Look up barcode", exact=True).click()
        results = page.locator(f"#mbid-results-{album.id}")
        playwright_sync.expect(results.get_by_role("button", name="Use", exact=True)).to_have_count(
            2
        )
        assert sidecar.read(album.path).mb_match_candidate is None
        with page.expect_response(lambda r: r.url.endswith("/assign")):
            results.get_by_role("button", name="Use", exact=True).first.click()
        playwright_sync.expect(page.get_by_role("heading", name="Suggested match:")).to_be_visible()
        playwright_sync.expect(
            page.get_by_role("button", name="Confirm suggestion", exact=True)
        ).to_be_visible()
        assert sidecar.read(album.path).mb_release_id is None
        assert file.read_bytes() == before
        browser.close()
