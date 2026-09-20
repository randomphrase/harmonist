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
        pw.expect(finding).to_contain_text("2 → 4")
        pw.expect(finding.get_by_role("button", name="Apply updates")).to_have_count(0)
        review = finding.get_by_role("button", name="Review assignments")
        review.click()
        editor = page.locator("#album-track-editor")
        pw.expect(editor.locator("[data-assignment-row]")).to_have_count(4)
        pw.expect(editor.get_by_role("checkbox", name="Use artwork")).to_have_count(0)
        pw.expect(page.get_by_role("dialog")).not_to_be_visible()
        editor.get_by_role("button", name="Cancel", exact=True).click()
        pw.expect(page.locator("#album-tracks .tracklist")).to_be_visible()
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
