"""Exercise the contribution check and Library filter through real HTMX."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
playwright_sync = pytest.importorskip("playwright.sync_api")

ALBUM = "demo-rel-dingoes"


def test_contribution_check_and_library_filter(contribution_server: tuple[str, bool]) -> None:
    server, stale = contribution_server
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        with page.expect_response(lambda r: r.url.endswith("/library/demo-rel-wyld/compare")):
            page.goto(f"{server}/album/demo-rel-wyld")
        playwright_sync.expect(page.locator(".album-contribution-area")).to_be_hidden()
        discoveries = []
        page.on(
            "request",
            lambda request: (
                discoveries.append(request.url)
                if request.url.endswith(f"/library/{ALBUM}/contributions/editions")
                else None
            ),
        )
        page.goto(f"{server}/album/{ALBUM}")
        panel = page.locator(f"#album-contributions-{ALBUM}")
        playwright_sync.expect(panel).to_contain_text("Possible media mismatch")
        playwright_sync.expect(panel).not_to_contain_text("Store URL missing from MusicBrainz")
        results = panel.locator(f"#contribution-editions-{ALBUM}")
        playwright_sync.expect(results.locator("tbody tr")).to_have_count(2)
        playwright_sync.expect(results.locator('a[href*="/release?url="]')).to_be_visible()
        assert len(discoveries) == 1
        # A second visit has a stored comparison. With the zero-TTL fixture
        # that comparison refreshes itself before it may discover siblings.
        if stale:
            with page.expect_response(
                lambda r: r.url.endswith(f"/library/{ALBUM}/compare?check=1"), timeout=10_000
            ) as refreshed:
                page.reload()
            assert refreshed.value.ok
        else:
            page.reload()
        playwright_sync.expect(results.locator("tbody tr")).to_have_count(2)
        assert len(discoveries) == 2
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
        playwright_sync.expect(results).to_contain_text("Bandcamp download")
        playwright_sync.expect(results).to_contain_text("Same URL")
        playwright_sync.expect(results).to_contain_text("Digital reissue")
        playwright_sync.expect(
            results.get_by_role("region", name="Digital editions")
        ).to_be_visible()
        playwright_sync.expect(results.locator("tbody tr")).to_have_count(2)
        assert len(discoveries) == 3
        playwright_sync.expect(results).to_contain_text("Not linked")
        # Review replaces the choices inline without changing the confirmed
        # match. Returning restores both the choices and the ordinary findings.
        suggestion = results.locator("tbody tr").filter(has_text="Suggested")
        playwright_sync.expect(suggestion).to_contain_text("Bandcamp download")
        suggestion.get_by_role("button", name="Use", exact=True).click()
        review = page.get_by_role("region", name="Review suggested release")
        playwright_sync.expect(review).to_contain_text("Suggested match")
        playwright_sync.expect(panel).to_be_hidden()
        playwright_sync.expect(page.locator(f"#album-findings-{ALBUM}")).to_be_hidden()
        playwright_sync.expect(
            review.get_by_role("button", name="Confirm suggestion", exact=True)
        ).to_be_enabled()
        selected_art = review.get_by_alt_text("Artwork for selected release", exact=True)
        playwright_sync.expect(selected_art).to_be_visible()
        page.wait_for_function(
            "() => { const img = document.querySelector('.contribution-review img[alt=\"Artwork for selected release\"]'); return img?.complete && img.naturalWidth > 0; }"
        )
        selected_art.click()
        popover_image = review.locator("[popover]:popover-open img")
        playwright_sync.expect(popover_image).to_be_visible()
        page.wait_for_function(
            "() => { const img = document.querySelector('.contribution-review [popover]:popover-open img'); return img?.complete && img.naturalWidth > 0; }"
        )
        page.keyboard.press("Escape")
        # A current-release refresh cannot tear down the replacement or browse
        # siblings again while the user is reviewing it.
        review.locator(".assignment-content").evaluate("e => e.dataset.reviewKept = 'true'")
        panel.evaluate("e => e.dataset.choicesKept = 'true'")
        with page.expect_response(lambda r: r.url.endswith(f"/library/{ALBUM}/compare?reread=1")):
            page.get_by_role(
                "button", name="Read this release from MusicBrainz again", exact=True
            ).click()
        playwright_sync.expect(
            page.get_by_role("button", name="Read this release from MusicBrainz again", exact=True)
        ).to_be_enabled()
        playwright_sync.expect(panel).to_have_attribute("data-choices-kept", "true")
        playwright_sync.expect(review.locator(".assignment-content")).to_have_attribute(
            "data-review-kept", "true"
        )
        assert len(discoveries) == 3
        review.get_by_role("button", name="Cancel", exact=True).click()
        playwright_sync.expect(review).to_be_hidden()
        playwright_sync.expect(panel).to_be_visible()
        playwright_sync.expect(panel).to_contain_text("Possible media mismatch")
        # An unlinked sibling is still a valid choice. Its missing URL becomes
        # actionable only after successful reviewed tagging.
        results.locator("tbody tr").filter(has_text="Digital reissue").get_by_role(
            "button", name="Use", exact=True
        ).click()
        review.get_by_role("button", name="Edit track assignments", exact=True).click()
        playwright_sync.expect(
            review.get_by_role("button", name="Cancel", exact=True)
        ).to_have_count(1)
        review.get_by_role("button", name="Accept changes", exact=True).click()
        playwright_sync.expect(
            review.get_by_role("button", name="Confirm suggestion", exact=True)
        ).to_be_enabled()
        with page.expect_response(lambda r: "/confirm/" in r.url and "/accept" in r.url) as tagged:
            review.get_by_role("button", name="Confirm suggestion", exact=True).click()
        assert "confirmation-applied" in tagged.value.headers.get("hx-trigger", "")
        page.wait_for_url("**/album/demo-rel-dingoes-reissue*")
        panel = page.locator("#album-contributions-demo-rel-dingoes-reissue")
        playwright_sync.expect(panel).to_contain_text("Store URL missing from MusicBrainz")
        playwright_sync.expect(panel).not_to_contain_text("Possible media mismatch")
        playwright_sync.expect(
            panel.get_by_role("textbox", name="Store URL to copy")
        ).to_have_value("https://dingoes.bandcamp.com/album/little-bit-o-hoot")
        assert (
            panel.get_by_role("link", name="Edit store link on MusicBrainz").get_attribute("href")
            == "https://musicbrainz.org/release/demo-rel-dingoes-reissue/edit"
        )
        # Refreshing the source observation clears transient discovery results.
        page.get_by_role(
            "button", name="Read this release from MusicBrainz again", exact=True
        ).click()
        playwright_sync.expect(panel).to_contain_text("Store URL missing from MusicBrainz")
        page.goto(f"{server}/?tab=library")
        page.evaluate("window.__contributionNoReload = true")
        page.get_by_role("navigation", name="Library filters").get_by_role(
            "link", name="MB contributions"
        ).click()
        page.wait_for_url("**/*filter=mb-contributions*")
        playwright_sync.expect(page.locator("#library-page")).to_contain_text("Little Bit o' Hoot")
        assert page.evaluate("window.__contributionNoReload === true")
        browser.close()
