"""The barcode picker must survive the album page's after-request handler."""

import os

import pytest
from test.e2e.conftest import review_dialog

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
playwright_sync = pytest.importorskip("playwright.sync_api")


def test_barcode_choices_open_review_without_tagging(browser_factory, barcode_server):
    server, root = barcode_server
    from harmonist import scanner, sidecar

    album = next(a for a in scanner.scan(root) if a.title == "Frequencies")
    file = album.path / "01 LFO.m4a"
    before = file.read_bytes()
    browser = browser_factory("chromium")
    page = browser.new_page()
    with page.expect_response(lambda r: r.url.endswith("/barcode")):
        page.goto(f"{server}/album/{album.id}")
    playwright_sync.expect(page.get_by_role("radio", name="By barcode", exact=True)).to_be_checked()
    playwright_sync.expect(page.get_by_role("button", name="Search", exact=True)).to_be_enabled()
    page.get_by_role("radio", name="By name", exact=True).check()
    playwright_sync.expect(page.get_by_placeholder("Artist", exact=True)).to_be_visible()
    results = page.locator(f"#mbid-results-{album.id}")
    with page.expect_response(lambda r: r.url.endswith("/search")):
        page.get_by_role("button", name="Search", exact=True).click()
    playwright_sync.expect(results.get_by_role("listitem")).to_have_count(2)
    playwright_sync.expect(results.get_by_role("listitem").first).to_contain_text("Frequencies")
    # Widen HTMX's initialization interval to catch fast-click races.
    page.evaluate("htmx.config.defaultSettleDelay = 1000")
    with page.expect_response(lambda r: r.url.endswith("/barcode")):
        page.get_by_role("radio", name="By barcode", exact=True).check()
    playwright_sync.expect(results.get_by_role("listitem")).to_have_count(2)
    playwright_sync.expect(results.get_by_role("listitem").first).to_contain_text("Barcode matches")
    playwright_sync.expect(results.get_by_role("button", name="Review", exact=True)).to_have_count(
        2
    )
    assert sidecar.read(album.path).mb_match_candidate is None
    with page.expect_response(lambda r: "/review/" in r.url):
        results.get_by_role("button", name="Review", exact=True).first.click()
    review = review_dialog(page)
    playwright_sync.expect(review.get_by_role("heading", name="Suggested match:")).to_be_visible()
    playwright_sync.expect(
        review.get_by_role("button", name="Confirm suggestion", exact=True)
    ).to_be_visible()
    assert sidecar.read(album.path).mb_release_id is None
    assert sidecar.read(album.path).mb_match_candidate is None
    assert file.read_bytes() == before
    page.context.close()


def test_automatic_barcode_outcomes(browser_factory, barcode_outcome_server, htmx_ready):
    server, root, outcome = barcode_outcome_server
    from harmonist import sidecar

    folder = root / "LFO" / "Frequencies"
    before = (folder / "01 LFO.m4a").read_bytes()
    browser = browser_factory("chromium")
    page = browser.new_page()
    searches = []
    page.on("request", lambda r: searches.append(r.url) if r.url.endswith("/barcode") else None)
    page.goto(f"{server}/album/barcode-demo")
    if outcome == "1":
        # The one release the barcode points to leads the list, marked as the
        # suggestion; a search stores nothing, so it waits for Review (#933).
        suggested = page.locator("#mbid-results-barcode-demo").get_by_role(
            "listitem", name="Suggested release"
        )
        playwright_sync.expect(suggested).to_contain_text("Barcode matches")
        assert sidecar.read(folder).mb_match_candidate is None
        htmx_ready(suggested.get_by_role("button", name="Review", exact=True)).click()
        review = review_dialog(page)
        playwright_sync.expect(
            review.get_by_role("heading", name="Suggested match:")
        ).to_be_visible()
        htmx_ready(review.get_by_role("button", name="Cancel", exact=True)).click()
        playwright_sync.expect(review).to_have_count(0)
        assert sidecar.read(folder).mb_match_candidate is None
    else:
        results = page.locator("#mbid-results-barcode-demo")
        if outcome == "0":
            playwright_sync.expect(results.get_by_text("No matches.", exact=True)).to_be_visible()
            link = results.get_by_role("link", name="Open in Harmony")
            playwright_sync.expect(link).to_have_attribute(
                "href",
                "https://harmony.pulsewidth.org.uk/release?gtin=0801061000332&qobuz=&deezer=",
            )
        else:
            playwright_sync.expect(
                results.get_by_text("Barcode lookup failed", exact=False)
            ).to_be_visible()
        page.get_by_role("radio", name="By name", exact=True).check()
        playwright_sync.expect(page.get_by_placeholder("Artist", exact=True)).to_be_visible()
    assert len(searches) == 1
    assert sidecar.read(folder).mb_release_id is None
    assert (folder / "01 LFO.m4a").read_bytes() == before
    page.context.close()
