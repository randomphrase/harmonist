"""Exercise the contribution check and Library filter through real HTMX."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
playwright_sync = pytest.importorskip("playwright.sync_api")

ALBUM = "demo-rel-dingoes"


def test_contribution_check_and_library_filter(contribution_server: str) -> None:
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(f"{contribution_server}/album/{ALBUM}")
        panel = page.locator(f"#album-contributions-{ALBUM}")
        playwright_sync.expect(panel).to_contain_text("Possible media mismatch")
        playwright_sync.expect(panel).to_contain_text("Store URL missing from MusicBrainz")
        assert "url=https" in panel.get_by_role("link", name="Open in Harmony").get_attribute(
            "href"
        )
        # Replace the displayed finding so a successful request alone cannot
        # pass: the header refresh must actually update this section too.
        panel.get_by_text("Possible media mismatch.", exact=True).evaluate(
            "e => e.textContent = 'Previous contribution finding'"
        )
        with page.expect_response(
            lambda r: (
                r.url.endswith(f"/library/{ALBUM}/compare?reread=1") and r.request.method == "GET"
            ),
            timeout=10_000,
        ) as checked:
            page.get_by_role(
                "button", name="Read this release from MusicBrainz again", exact=True
            ).click()
        assert checked.value.status == 200
        playwright_sync.expect(panel).to_contain_text("Possible media mismatch")
        playwright_sync.expect(
            page.get_by_role("button", name="Read this release from MusicBrainz again", exact=True)
        ).to_be_enabled()
        page.goto(f"{contribution_server}/?tab=library")
        page.evaluate("window.__contributionNoReload = true")
        page.get_by_role("navigation", name="Library filters").get_by_role(
            "link", name="MB contributions"
        ).click()
        page.wait_for_url("**/*filter=mb-contributions*")
        playwright_sync.expect(page.locator("#library-page")).to_contain_text("Little Bit o' Hoot")
        assert page.evaluate("window.__contributionNoReload === true")
        playwright_sync.expect(page.locator("#contribution-coverage")).to_contain_text(
            "fully checked"
        )
        browser.close()
