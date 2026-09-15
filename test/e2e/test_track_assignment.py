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


def test_column_display_toggle_survives_moves_and_reset(reset_demo_server):
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        card = _card(page, reset_demo_server)
        card.get_by_role("button", name="Edit track assignments").click()
        editor = card.locator(".assignment-editor")
        filenames = editor.get_by_role("radio", name="Filename", exact=True)
        pw.expect(editor.locator("[data-assignment-row] .assignment-title").first).to_be_visible()
        pw.expect(
            editor.locator("[data-assignment-row] .assignment-filename").first
        ).not_to_be_visible()
        filenames.focus()
        filenames.press("Space")
        pw.expect(
            editor.locator("[data-assignment-row] .assignment-filename").first
        ).to_be_visible()
        pw.expect(
            editor.locator("[data-assignment-row] .assignment-title").first
        ).not_to_be_visible()
        editor.get_by_role("button", name="Move on-disk entry down", exact=True).first.click()
        pw.expect(editor.locator('[name="disk_order"]')).to_have_value("1,0,2")
        pw.expect(filenames).to_be_checked()
        pw.expect(
            editor.locator("[data-assignment-row] .assignment-filename").first
        ).to_be_visible()
        editor.locator("label.assignment-display-choice", has_text="Title").click()
        pw.expect(editor.locator("[data-assignment-row] .assignment-title").first).to_be_visible()
        editor.get_by_role("button", name="Reset", exact=True).click()
        pw.expect(editor.locator('[name="disk_order"]')).to_have_value("0,1,2")
        editor.get_by_role("button", name="Cancel", exact=True).click()
        pw.expect(card.get_by_role("button", name="Confirm release", exact=True)).to_be_visible()
        # Read-only mode keeps the same table geometry and display preference.
        columns = editor.locator("thead tr").last.locator("th")
        positions = columns.evaluate_all("els => els.map(el => el.getBoundingClientRect().x)")
        filenames.focus()
        filenames.press("Space")
        pw.expect(editor.locator(".assignment-filename").first).to_be_visible()
        editor.get_by_role("button", name="Edit track assignments").click()
        pw.expect(editor.get_by_role("button", name="Accept changes")).to_be_visible()
        pw.expect(filenames).to_be_checked()
        assert (
            columns.evaluate_all("els => els.map(el => el.getBoundingClientRect().x)") == positions
        )
        editor.get_by_role("button", name="Cancel", exact=True).click()
        editor.get_by_role(
            "button", name="Read this release from MusicBrainz again and reset assignment changes"
        ).click()
        pw.expect(editor.get_by_role("button", name="Edit track assignments")).to_be_visible()
        pw.expect(editor.get_by_role("button", name="Move on-disk entry down")).to_have_count(0)
        browser.close()


def test_partial_confirmation_can_be_applied_and_reviewed(reset_demo_server):
    base = reset_demo_server
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        card = _card(page, base)
        aid = card.get_attribute("id").removeprefix("task-")
        # Three files against a two-track release leaves one file unassigned.
        assert page.request.post(
            f"{base}/manual/{aid}/assign",
            headers={"HX-Request": "true"},
            form={"mbid": "demo-rel-folksmen"},
        ).ok
        page.reload()
        card = page.locator(f"#task-{aid}")
        card.get_by_role("button", name="Confirm release", exact=True).click()
        dialog = page.get_by_role("dialog")
        pw.expect(dialog).to_contain_text("Tracks unassigned")
        dialog.get_by_role("button", name="Confirm release and apply changes", exact=True).click()
        pw.expect(dialog).not_to_be_visible()
        page.goto(f"{base}/album/demo-rel-folksmen")
        pw.expect(page.locator("main")).to_contain_text("Tracks unassigned")
        page.get_by_role("button", name="Review assignments", exact=True).click()
        dialog = page.get_by_role("dialog")
        pw.expect(dialog).to_be_visible()
        pw.expect(dialog.locator('[name="disk_order"]')).to_have_value("0,1,2")
        dialog.get_by_role("button", name="Move on-disk entry up", exact=True).last.click()
        pw.expect(dialog.locator('[name="disk_order"]')).to_have_value("0,2,1")
        dialog.get_by_role(
            "button", name="Read this release from MusicBrainz again and reset assignment changes"
        ).click()
        pw.expect(dialog.locator('[name="disk_order"]')).to_have_value("0,1,2")
        browser.close()


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
            card.get_by_role("button", name="Confirm release", exact=True)
        ).not_to_be_visible()
        # A normal inbox poll must not reset a corrected draft.
        with page.expect_response(lambda r: "/tasks" in r.url):
            page.evaluate("htmx.trigger(document.body, 'tasks-changed')")
        pw.expect(editor.locator('[name="disk_order"]')).to_have_value("1,0,2")
        editor.get_by_role("button", name="Accept changes").click()
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
        editor.get_by_role("button", name="Cancel").click()
        editor.get_by_role("button", name="Edit track assignments").click()
        pw.expect(editor.locator('[name="disk_order"]')).to_have_value("0,1,2,-")
        pw.expect(editor.locator('[name="mb_order"]')).to_have_value("0,1,2,3")
        browser.close()
