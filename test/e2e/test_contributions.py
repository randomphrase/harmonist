"""Exercise the contribution check and Library filter through real HTMX."""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
playwright_sync = pytest.importorskip("playwright.sync_api")

ALBUM = "demo-rel-dingoes"


def test_digital_store_link_waits_for_siblings(digital_contribution_server):
    server, scenario = digital_contribution_server
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        pending = []
        page.route("**/contributions/editions", lambda route: pending.append(route))
        page.goto(f"{server}/album/{ALBUM}")
        panel = page.locator(f"#album-contributions-{ALBUM}")
        editor = panel.get_by_role("link", name="Edit store link on MusicBrainz")
        for visit in range(2):
            if visit:
                # An expired comparison must finish its refresh before discovery.
                with page.expect_response(lambda r: r.url.endswith("/compare?check=1")):
                    page.reload()
            playwright_sync.expect(panel).to_contain_text("Checking releases on MusicBrainz")
            playwright_sync.expect(editor).to_have_count(0)
            # Pump browser events until the intercepted request is delivered.
            page.wait_for_function(
                "() => document.querySelector('[id^=\"contribution-editions-\"].htmx-request') !== null"
            )
            assert len(pending) == visit + 1
            pending[-1].continue_()
            results = panel.locator(f"#contribution-editions-{ALBUM}")
            # Releases are listed only while the match is in question (#618),
            # the current match first.
            playwright_sync.expect(
                results.get_by_role("list", name="Release candidates").get_by_role("listitem")
            ).to_have_count(3 if scenario == "linked" else 0)
            if scenario == "linked":
                playwright_sync.expect(results).to_contain_text(
                    "Your store URL is linked from another release, not the matched one"
                )
                playwright_sync.expect(
                    results.get_by_role("listitem", name="Suggested digital release")
                ).to_contain_text("Bandcamp download")
                # Accepting the original release must not offer the URL again.
                playwright_sync.expect(editor).to_have_count(0)
                playwright_sync.expect(panel.get_by_role("link", name="Add Release")).to_have_count(
                    0
                )
                playwright_sync.expect(panel).not_to_contain_text(
                    "Store URL missing from this release", use_inner_text=True
                )
            else:
                playwright_sync.expect(editor).to_be_visible()
                playwright_sync.expect(panel.get_by_role("link", name="Add Release")).to_have_count(
                    0
                )
                playwright_sync.expect(panel).to_contain_text("Store URL missing from this release")
                assert (
                    editor.get_attribute("href") == f"https://musicbrainz.org/release/{ALBUM}/edit"
                )
            assert len(pending) == visit + 1
        browser.close()


def test_dismissed_store_match_does_not_offer_a_duplicate_url(
    digital_contribution_server, htmx_ready
):
    server, scenario = digital_contribution_server
    if scenario != "linked":
        pytest.skip("requires another release linking the exact URL")
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(f"{server}/album/{ALBUM}")
        panel = page.locator(f"#album-contributions-{ALBUM}")
        box = htmx_ready(panel.get_by_role("checkbox", name="Don't warn me about this"))
        reason = panel.get_by_text(
            "Your store URL is linked from another release, not the matched one.", exact=True
        )
        editor = panel.get_by_role("link", name="Edit store link on MusicBrainz")
        playwright_sync.expect(reason).to_be_visible()
        with page.expect_response(lambda r: r.url.endswith("/release-accepted")) as saved:
            box.check()
        assert saved.value.ok
        playwright_sync.expect(reason).to_be_hidden()
        playwright_sync.expect(editor).to_have_count(0)
        page.reload()
        playwright_sync.expect(box).to_be_checked()
        playwright_sync.expect(reason).to_be_hidden()
        playwright_sync.expect(editor).to_have_count(0)
        with page.expect_response(lambda r: r.url.endswith("/release-accepted")) as saved:
            box.uncheck()
        assert saved.value.ok
        playwright_sync.expect(reason).to_be_visible()
        playwright_sync.expect(editor).to_have_count(0)
        browser.close()


def test_contribution_actions_and_review_progress_layout(digital_contribution_server):
    server, scenario = digital_contribution_server
    if scenario != "linked":
        # Browse and the release choices belong to the mismatch review (#618);
        # a plain missing link shows only its edit, covered above.
        pytest.skip("no release choices without a possible mismatch")
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(f"{server}/album/{ALBUM}")
        panel = page.locator(f"#album-contributions-{ALBUM}")
        mismatch = panel.locator("section").filter(has_text="Possible mismatch")
        browse = panel.get_by_role("link", name="Browse all releases on MusicBrainz")
        progress = panel.locator('[role="status"]')
        playwright_sync.expect(browse).to_be_visible()
        for width in (1360, 390):
            page.set_viewport_size({"width": width, "height": 900})
            bounds = mismatch.bounding_box()
            assert bounds is not None
            box = browse.bounding_box()
            assert box is not None
            assert box["x"] >= bounds["x"]
            assert box["x"] + box["width"] <= bounds["x"] + bounds["width"]
            browse.focus()
            playwright_sync.expect(browse).to_be_focused()
            # No blank line held for an idle progress indicator.
            assert progress.evaluate("el => el.getBoundingClientRect().height") == 0
            action_bottom = browse.evaluate("el => el.getBoundingClientRect().bottom")
            padding = mismatch.evaluate("el => parseFloat(getComputedStyle(el).paddingBottom)")
            assert abs(bounds["y"] + bounds["height"] - action_bottom - padding - 1) <= 1
        if scenario != "empty":
            pending = []
            page.route("**/assignments/**", lambda route: pending.append(route))
            panel.get_by_role("button", name="Use", exact=True).first.click()
            playwright_sync.expect(progress).to_be_visible()
            page.wait_for_function(
                "() => document.querySelector('[id^=\"edition-reviewing-\"].htmx-request') !== null"
            )
            assert len(pending) == 1
            assert progress.evaluate("el => el.getBoundingClientRect().height") > 0
            pending[0].continue_()
            review = page.get_by_role("region", name="Review suggested release")
            playwright_sync.expect(review).to_be_visible()
            playwright_sync.expect(panel).to_be_hidden()
            page.unroute("**/assignments/**")
            review.get_by_role("button", name="Cancel", exact=True).click()
            playwright_sync.expect(browse).to_be_visible()
            assert progress.evaluate("el => el.getBoundingClientRect().height") == 0
        browser.close()


def test_sibling_without_release_cover_keeps_local_artwork(sibling_artwork_server):
    server, scenario = sibling_artwork_server
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(f"{server}/album/{ALBUM}")
        pending = []
        page.route("**/assignments/*/artwork?*", lambda route: pending.append(route))
        results = page.locator(f"#contribution-editions-{ALBUM}")
        results.get_by_role("listitem", name="Suggested digital release").get_by_role(
            "button", name="Use", exact=True
        ).click()
        review = page.get_by_role("region", name="Review suggested release")
        artwork = review.locator(".assignment-artwork")
        local = artwork.locator('img[alt^="Current artwork:"]')
        playwright_sync.expect(local.first).to_be_visible()
        confirm = review.get_by_role("button", name="Confirm suggestion", exact=True)
        before_y = confirm.evaluate("e => e.getBoundingClientRect().top + window.scrollY")
        assert pending
        for route in pending:
            route.continue_()
        page.unroute("**/assignments/*/artwork?*")
        expected = "Artwork could not be loaded" if scenario == "failure" else "No front cover"
        playwright_sync.expect(artwork).to_contain_text(expected)
        playwright_sync.expect(local.first).to_be_visible()
        assert confirm.evaluate(
            "e => e.getBoundingClientRect().top + window.scrollY"
        ) == pytest.approx(before_y, abs=1)
        playwright_sync.expect(
            artwork.get_by_role("checkbox", name="Use artwork from the selected release")
        ).to_have_count(0)
        playwright_sync.expect(
            artwork.get_by_alt_text("Artwork for selected release")
        ).to_have_count(0)
        # The album's current artwork is available even when the CAA check failed.
        before = page.locator("#album-artwork .art-row__side:not(.art-row__side--after) img")
        playwright_sync.expect(before.first).to_be_attached()
        digests = before.evaluate_all("els => els.map(e => e.src.split('?')[0].split('/').pop())")
        with page.expect_response(lambda r: "/artwork?" in r.url and "reread=true" in r.url):
            artwork.get_by_role(
                "button", name="Ask the Cover Art Archive about this release again"
            ).click()
        playwright_sync.expect(artwork).to_contain_text(expected)
        with page.expect_response(lambda r: "/confirm/" in r.url and "/accept" in r.url) as tagged:
            review.get_by_role("button", name="Confirm suggestion", exact=True).click()
        assert "confirmation-applied" in tagged.value.headers.get("hx-trigger", "")
        page.wait_for_url("**/album/demo-rel-dingoes-digital*")
        current = page.locator("#album-artwork .art-row__side:not(.art-row__side--after) img")
        playwright_sync.expect(current.first).to_be_visible()
        assert set(
            current.evaluate_all("els => els.map(e => e.src.split('?')[0].split('/').pop())")
        ) == set(digests)
        browser.close()


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
        linked = "Your store URL is linked from another release, not the matched one"
        playwright_sync.expect(panel).to_contain_text(linked)
        # The URL on another release, and a CD match for a download (#618).
        playwright_sync.expect(panel.locator("li > strong")).to_have_count(2)
        playwright_sync.expect(panel).not_to_contain_text(
            "Store URL missing from this release", use_inner_text=True
        )
        results = panel.locator(f"#contribution-editions-{ALBUM}")
        rows = results.get_by_role("list", name="Release candidates").get_by_role("listitem")
        playwright_sync.expect(rows).to_have_count(3)
        playwright_sync.expect(results.get_by_role("link", name="Add Release")).to_have_count(0)
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
        playwright_sync.expect(rows).to_have_count(3)
        assert len(discoveries) == 2
        # Replace the displayed finding so a successful request alone cannot
        # pass: the header refresh must actually update this section too.
        panel.get_by_text(f"{linked}.", exact=True).evaluate(
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
        playwright_sync.expect(panel).to_contain_text(linked)
        playwright_sync.expect(panel.locator("li > strong")).to_have_count(2)
        playwright_sync.expect(
            page.get_by_role("button", name="Read this release from MusicBrainz again", exact=True)
        ).to_be_enabled()
        playwright_sync.expect(results).to_contain_text("Bandcamp download")
        playwright_sync.expect(results).to_contain_text("Store URL matches")
        playwright_sync.expect(results).to_contain_text("Digital reissue")
        playwright_sync.expect(
            results.get_by_role("region", name="Releases in this release group")
        ).to_be_visible()
        playwright_sync.expect(rows).to_have_count(3)
        assert len(discoveries) == 3
        playwright_sync.expect(
            results.get_by_role("listitem")
            .filter(has_text="Digital reissue")
            .get_by_text("Store URL matches", exact=True)
        ).to_have_count(0)
        # Review replaces the choices inline without changing the confirmed
        # match. Returning restores both the choices and the ordinary findings.
        suggestion = results.get_by_role("listitem", name="Suggested digital release")
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
        playwright_sync.expect(panel).to_contain_text(linked)
        playwright_sync.expect(panel.locator("li > strong")).to_have_count(2)
        # An unlinked sibling is still a valid choice. After tagging, its missing
        # URL must trigger discovery again before any link edit is offered.
        results.get_by_role("listitem").filter(has_text="Digital reissue").get_by_role(
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
        selected_art = review.get_by_alt_text("Artwork for selected release")
        playwright_sync.expect(selected_art).to_be_visible()
        selected_digest = selected_art.get_attribute("src").split("?")[0].rsplit("/", 1)[-1]
        review.get_by_role("checkbox", name="Use artwork from the selected release").check()
        with page.expect_response(lambda r: "/confirm/" in r.url and "/accept" in r.url) as tagged:
            review.get_by_role("button", name="Confirm suggestion", exact=True).click()
        assert "confirmation-applied" in tagged.value.headers.get("hx-trigger", "")
        page.wait_for_url("**/album/demo-rel-dingoes-reissue*")
        current_art = page.locator("#album-artwork .art-row__side:not(.art-row__side--after) img")
        playwright_sync.expect(current_art.first).to_be_visible()
        assert set(
            current_art.evaluate_all("els => els.map(e => e.src.split('?')[0].split('/').pop())")
        ) == {selected_digest}
        # Choosing and confirming the reissue said it is the release bought
        # (#618): its warning starts dismissed. The URL still belongs to the
        # other release, so accepting this one must not offer a duplicate link.
        panel = page.locator("#album-contributions-demo-rel-dingoes-reissue")
        playwright_sync.expect(
            panel.get_by_role("checkbox", name="Don't warn me about this")
        ).to_be_checked()
        playwright_sync.expect(
            panel.get_by_role("listitem", name="Suggested digital release")
        ).to_be_hidden()
        playwright_sync.expect(panel).not_to_contain_text(
            "Store URL missing from this release", use_inner_text=True
        )
        playwright_sync.expect(
            panel.get_by_role("link", name="Edit store link on MusicBrainz")
        ).to_have_count(0)
        # A fresh observation still renders the same decision.
        page.get_by_role(
            "button", name="Read this release from MusicBrainz again", exact=True
        ).click()
        playwright_sync.expect(
            panel.get_by_role("link", name="Edit store link on MusicBrainz")
        ).to_have_count(0)
        page.goto(f"{server}/?tab=library")
        # Zero-count filters are text, not clickable links.
        playwright_sync.expect(
            page.get_by_role("navigation", name="Library filters")
        ).to_contain_text(re.compile(r"MB contributions\s+0"))
        browser.close()
