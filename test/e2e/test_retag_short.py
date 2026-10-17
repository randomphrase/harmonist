"""A tracklist update enters the inline editor; a generic re-tag cannot bypass it."""

from __future__ import annotations

import os
from urllib.parse import parse_qs

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")
ALBUM_ID = "demo-rel-wonders"


def test_changed_track_count_opens_review_and_cancel_preserves_finding(reset_demo_server):
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.goto(f"{reset_demo_server}/album/{ALBUM_ID}")
        finding = page.locator(f"#album-update-{ALBUM_ID}")
        pw.expect(finding.locator("h2")).to_have_text("Track assignments need review")
        pw.expect(finding.get_by_role("button", name="Apply updates")).to_have_count(0)
        review = finding.get_by_role("button", name="Review assignments")
        editor = page.locator("#album-track-editor")
        pw.expect(editor.locator("[data-assignment-row]")).to_have_count(4)
        pw.expect(editor.locator("[data-structural-reason]")).to_have_text(
            ["Disc 1: track total 2 on disk → 4 on MusicBrainz."]
        )
        pw.expect(editor.get_by_role("checkbox", name="Use artwork")).to_have_count(0)
        pw.expect(page.get_by_role("dialog")).not_to_be_visible()
        # The automatic open counts as this visit's open, as a click would.
        pw.expect(editor).to_have_attribute("data-review-opened", "true")
        editor.get_by_role("button", name="Cancel", exact=True).click()
        pw.expect(page.locator("#album-tracks .tracklist")).to_be_visible()
        # A comparison refresh must respect Cancel instead of opening again (#776).
        # The automatic open is issued while HTMX processes the refreshed Tracks
        # view, so once that refresh has settled it has either been sent or not.
        # Asserting on the editor alone raced the reopen and usually won.
        with page.expect_response(lambda r: "/compare" in r.url) as refreshed:
            opens = page.evaluate(
                """id => new Promise(resolve => {
                    let opens = 0;
                    const count = event => {
                        if (event.detail.target.id === 'album-track-editor') opens++;
                    };
                    document.body.addEventListener('htmx:beforeSend', count);
                    document.body.addEventListener('htmx:afterSettle', function settled(event) {
                        if (event.detail.target.id !== 'compare-' + id) return;
                        document.body.removeEventListener('htmx:afterSettle', settled);
                        document.body.removeEventListener('htmx:beforeSend', count);
                        resolve(opens);
                    });
                    htmx.ajax('GET', '/library/' + id + '/compare',
                        {target: '#compare-' + id, swap: 'innerHTML'});
                })""",
                ALBUM_ID,
            )
        # The refresh still asks for the automatic open; Cancel is what refuses it.
        assert 'hx-trigger="click, load' in refreshed.value.text()
        assert opens == 0
        pw.expect(page.locator("#album-tracks .tracklist")).to_be_visible()
        pw.expect(editor.locator(".assignment-content")).to_have_count(0)
        pw.expect(review).to_be_enabled()
        review.click()
        with page.expect_response(
            lambda r: r.url.endswith(f"/confirm/{ALBUM_ID}/accept")
        ) as applied:
            editor.get_by_role("button", name="Accept changes").click()
        fields = parse_qs(applied.value.request.post_data)
        assert fields["incomplete"] == ["true"]
        assert fields["include_artwork"] == ["false"]
        pw.expect(page.locator("main")).to_contain_text("2 of 4 tracks on disk")
        pw.expect(finding.get_by_role("button", name="Review assignments")).to_have_count(0)
        browser.close()


def test_combined_pregap_is_a_cancelable_assignment_choice(pregap_server, htmx_ready):
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.goto(f"{pregap_server}/album/demo-rel-electric-mayhem")
        edit = page.get_by_role("button", name="Edit track assignments", exact=True)
        htmx_ready(edit).click()
        editor = page.locator("#album-track-editor")
        box = editor.get_by_role("checkbox", name="Track 1 includes the pregap", exact=True)
        pw.expect(box).not_to_be_checked()
        rows = editor.locator("[data-assignment-row]")
        pw.expect(rows).to_have_count(3)
        pw.expect(rows.first).to_contain_text("Missing on disk")
        box.focus()
        box.press("Space")
        pw.expect(box).to_be_checked()
        pw.expect(rows).to_have_count(2)
        pw.expect(box).to_be_focused()
        first = rows.first
        pw.expect(first.locator("td").nth(3)).to_have_text("0+1")
        included = editor.locator("[data-included-pregap]")
        pw.expect(included.locator("td").nth(0)).to_have_text("Includes pregap: Hidden Prelude")
        pw.expect(included.locator("td").nth(1)).to_have_text("0:10")
        pw.expect(first.locator("td").nth(5)).to_have_text("0:30")
        pw.expect(first).not_to_contain_text("Δ")
        box.uncheck()
        pw.expect(rows).to_have_count(3)
        pw.expect(rows.first).to_contain_text("Missing on disk")
        pw.expect(rows.nth(1)).to_contain_text("Δ 10s")
        box.check()
        pw.expect(rows).to_have_count(2)
        editor.get_by_role("button", name="Cancel", exact=True).click()
        htmx_ready(edit).click()
        pw.expect(box).not_to_be_checked()
        box.check()
        pw.expect(rows).to_have_count(2)
        editor.get_by_role("button", name="Accept changes", exact=True).click()
        note = page.locator("#album-track-view [data-pregap-note]")
        pw.expect(note).to_have_text("Includes pregap: Hidden Prelude")
        note_length = page.locator("#album-track-view [data-pregap-row] .track-diff__len")
        pw.expect(note_length).to_have_text("0:10")
        pw.expect(note).to_be_visible()
        pw.expect(page.locator("main")).not_to_contain_text("2 of 3 tracks on disk")
        page.reload()
        pw.expect(note).to_have_text("Includes pregap: Hidden Prelude")
        pw.expect(note_length).to_have_text("0:10")
        htmx_ready(edit).click()
        pw.expect(box).to_be_checked()
        pw.expect(rows).to_have_count(2)
        box.uncheck()
        pw.expect(rows).to_have_count(3)
        editor.get_by_role("button", name="Accept changes", exact=True).click()
        pw.expect(page.locator("main")).to_contain_text("2 of 3 tracks on disk")
        browser.close()


def test_generic_apply_redirects_into_review_even_with_short_override(reset_demo_server):
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.goto(f"{reset_demo_server}/album/{ALBUM_ID}")
        pw.expect(page.get_by_role("button", name="Review assignments")).to_be_visible()
        with page.expect_response(lambda r: r.url.endswith(f"/retag/{ALBUM_ID}")) as refused:
            page.evaluate(
                """id => htmx.ajax('POST', '/retag/' + id,
                {target: '#modal', swap: 'none', values: {accept_short: 'true'}})""",
                ALBUM_ID,
            )
        assert "edit_assignments=true" in refused.value.headers["hx-redirect"]
        pw.expect(page.locator("#album-track-editor [data-assignment-row]")).to_have_count(4)
        pw.expect(page.get_by_role("dialog")).not_to_be_visible()
        page.locator("#album-track-editor").get_by_role("button", name="Cancel", exact=True).click()
        pw.expect(page.get_by_role("button", name="Review assignments")).to_be_visible()
        browser.close()


def test_absent_pregap_does_not_open_review_but_remains_visible(pregap_server):
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.goto(f"{pregap_server}/album/demo-rel-electric-mayhem")
        pw.expect(page.locator("main")).to_contain_text("2 of 3 tracks on disk")
        tracklist = page.locator("#album-tracks .tracklist")
        pw.expect(tracklist).to_contain_text("Hidden Prelude")
        pw.expect(tracklist).to_be_visible()
        editor = page.locator("#album-track-editor")
        pw.expect(editor.locator(".assignment-content")).to_have_count(0)
        page.get_by_role("button", name="Edit track assignments", exact=True).click()
        rows = editor.locator("[data-assignment-row]")
        pw.expect(rows).to_have_count(3)
        pw.expect(rows.first.locator("td").nth(1)).to_contain_text("Missing on disk")
        pw.expect(rows.first.locator("td").nth(3)).to_have_text("0")
        pw.expect(editor.locator("[data-structural-reason]")).to_have_count(0)
        editor.get_by_role("button", name="Cancel", exact=True).click()
        pw.expect(tracklist).to_be_visible()
        browser.close()
