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
        with page.expect_response(
            lambda r: r.url.endswith(f"/library/{ALBUM}/contributions/editions"),
            timeout=10_000,
        ) as discovered:
            panel.get_by_role("button", name="Find digital editions").click()
        assert discovered.value.status == 200
        results = panel.locator(f"#contribution-editions-{ALBUM}")
        playwright_sync.expect(results).to_contain_text("Bandcamp download")
        playwright_sync.expect(results).to_contain_text("Links to this download")
        playwright_sync.expect(results).to_contain_text("Digital reissue")
        playwright_sync.expect(results).to_contain_text("Store link missing")
        playwright_sync.expect(results.get_by_role("listitem")).to_have_count(2)
        # Refreshing the source observation clears transient discovery results.
        page.get_by_role(
            "button", name="Read this release from MusicBrainz again", exact=True
        ).click()
        playwright_sync.expect(results).to_be_empty()
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
