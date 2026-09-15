"""Arrows, draft preservation and confirmation must work in a real browser."""

import os
from urllib.parse import parse_qs

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")


def _card(page, base):
    page.goto(base)
    card = page.locator('div[id^="task-"].relative').filter(has_text="Gimme Some Money")
    pw.expect(card).to_have_count(1)
    return card


def test_arrow_mapping_survives_refresh_review_and_apply(reset_demo_server):
    base = reset_demo_server
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        card = _card(page, base)
        aid = card.get_attribute("id").removeprefix("task-")
        card.get_by_role("button", name="Edit track assignments").click()
        editor = card.locator(".assignment-editor")
        down = editor.get_by_role("button", name="Move on-disk entry down", exact=True).first
        down.focus()
        down.press("Enter")
        pw.expect(editor.locator('[name="disk_order"]')).to_have_value("1,0,2")
        pw.expect(editor.locator(f'[id="move-{aid}-disk-0-down"]')).to_be_focused()
        pw.expect(
            card.get_by_role("button", name="Review and confirm release", exact=True)
        ).not_to_be_visible()
        # A normal inbox poll must not reset a corrected draft.
        with page.expect_response(lambda r: "/tasks" in r.url):
            page.evaluate("htmx.trigger(document.body, 'tasks-changed')")
        pw.expect(editor.locator('[name="disk_order"]')).to_have_value("1,0,2")
        editor.get_by_role("button", name="Review assigned tracks").click()
        dialog = page.get_by_role("dialog")
        pw.expect(dialog).to_be_visible()
        pw.expect(dialog.locator('[name="disk_order"]')).to_have_value("1,0,2")
        with page.expect_response(lambda r: r.url.endswith(f"/confirm/{aid}")) as applied:
            dialog.get_by_role(
                "button", name="Confirm release and apply changes", exact=True
            ).click()
        assert parse_qs(applied.value.request.post_data)["disk_order"] == ["1,0,2"]
        assert "confirmation-applied" in applied.value.headers.get("hx-trigger", "")
        pw.expect(dialog).not_to_be_visible()
        browser.close()


def test_gap_can_move_on_either_side_and_cancel_discards_the_draft(reset_demo_server):
    base = reset_demo_server
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        card = _card(page, base)
        aid = card.get_attribute("id").removeprefix("task-")
        # Three local files against the demo's four-track release.
        assert page.request.post(
            f"{base}/manual/{aid}/assign",
            headers={"HX-Request": "true"},
            form={"mbid": "demo-rel-electric-mayhem"},
        ).ok
        page.reload()
        card = page.locator(f"#task-{aid}")
        card.get_by_role("button", name="Edit track assignments").click()
        editor = card.locator(".assignment-editor")
        pw.expect(editor.locator('[name="disk_order"]')).to_have_value("0,1,2,-")
        editor.get_by_role("button", name="Move on-disk entry up", exact=True).last.click()
        pw.expect(editor.locator('[name="disk_order"]')).to_have_value("0,1,-,2")
        pw.expect(editor.locator(f'[id="move-{aid}-disk-gap-0-up"]')).to_be_focused()
        pw.expect(editor.locator('[data-assignment-row="2"]')).to_contain_text("Missing on disk")
        editor.get_by_role("button", name="Move MusicBrainz entry up", exact=True).last.click()
        pw.expect(editor.locator('[name="mb_order"]')).to_have_value("0,1,3,2")
        editor.get_by_role("button", name="Cancel assignment changes").click()
        editor.get_by_role("button", name="Edit track assignments").click()
        pw.expect(editor.locator('[name="disk_order"]')).to_have_value("0,1,2,-")
        pw.expect(editor.locator('[name="mb_order"]')).to_have_value("0,1,2,3")
        browser.close()
