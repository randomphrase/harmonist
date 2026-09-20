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
        dialog.get_by_role("button", name="Confirm release", exact=True).click()
        pw.expect(dialog).not_to_be_visible()
        page.goto(f"{base}/album/demo-rel-folksmen")
        pw.expect(page.locator("main")).to_contain_text("Tracks unassigned")
        page.locator("#album-tracks").get_by_role(
            "button", name="Edit track assignments", exact=True
        ).click()
        dialog = page.locator("#album-track-editor")
        pw.expect(dialog).to_be_visible()
        pw.expect(dialog.locator('[name="disk_order"]')).to_have_value("0,1,2")
        dialog.get_by_role("button", name="Move on-disk entry up", exact=True).last.click()
        pw.expect(dialog.locator('[name="disk_order"]')).to_have_value("0,2,1")
        dialog.get_by_role(
            "button", name="Read this release from MusicBrainz again and reset assignment changes"
        ).click()
        pw.expect(dialog.locator('[name="disk_order"]')).to_have_value("0,1,2")
        dialog.get_by_role("button", name="Accept changes").click()
        confirmation = page.locator("#confirmation-modal dialog")
        pw.expect(confirmation).to_be_visible()
        confirmation.get_by_role("button", name="Back", exact=True).click()
        pw.expect(confirmation).not_to_be_visible()
        pw.expect(dialog.get_by_role("button", name="Accept changes")).to_be_enabled()
        pw.expect(dialog.locator('[name="disk_order"]')).to_have_value("0,1,2")
        dialog.get_by_role("button", name="Cancel", exact=True).click()
        pw.expect(page.locator("#album-tracks .tracklist")).to_be_visible()
        pw.expect(page.get_by_role("dialog")).not_to_be_visible()
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
        page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")
        scan_before = page.request.get(f"{base}/scan/status").json()
        assert scan_before["state"] == "done"
        # Status timestamps have second precision; a fast unwanted scan in the
        # same second could otherwise leave an identical status response.
        page.wait_for_function(
            "finished => Date.now() >= Date.parse(finished) + 1100",
            arg=scan_before["finished_at"],
        )
        down.focus()
        down.press("Enter")
        pw.expect(editor.locator('[name="disk_order"]')).to_have_value("1,0,2")
        assert page.request.get(f"{base}/scan/status").json() == scan_before
        row = editor.locator('[data-assignment-row="0"]')
        note = row.locator("td").nth(0).locator('[aria-label^="Track numbering changes"]')
        pw.expect(note).to_have_text("2 → 1")
        assert note.evaluate("el => el.scrollWidth <= el.clientWidth")
        pw.expect(editor.locator(f'[id="move-{aid}-disk-0-down"]')).to_be_focused()
        pw.expect(
            card.get_by_role("button", name="Confirm release", exact=True)
        ).not_to_be_visible()
        # A normal inbox poll must not reset a corrected draft.
        with page.expect_response(lambda r: "/tasks" in r.url):
            page.evaluate("htmx.trigger(document.body, 'tasks-changed')")
        pw.expect(editor.locator('[name="disk_order"]')).to_have_value("1,0,2")
        # A complete tags-only review applies directly from the visible editor.
        with page.expect_response(lambda r: r.url.endswith(f"/confirm/{aid}/accept")) as applied:
            editor.get_by_role("button", name="Accept changes").click()
        assert parse_qs(applied.value.request.post_data)["disk_order"] == ["1,0,2"]
        assert "confirmation-applied" in applied.value.headers.get("hx-trigger", "")
        pw.expect(page.get_by_role("dialog")).not_to_be_visible()
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


def test_closing_partial_confirmation_restores_accept_button(reset_demo_server):
    base = reset_demo_server
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        card = _card(page, base)
        aid = card.get_attribute("id").removeprefix("task-")
        assert page.request.post(
            f"{base}/manual/{aid}/assign",
            headers={"HX-Request": "true"},
            form={"mbid": "demo-rel-folksmen"},
        ).ok
        page.reload()
        card = page.locator(f"#task-{aid}")
        card.get_by_role("button", name="Edit track assignments").click()
        editor = card.locator(".assignment-editor")
        before = editor.locator('[name="disk_order"]').input_value()
        accept = editor.get_by_role("button", name="Accept changes")

        def poll_during_accept(route):
            response = route.fetch()
            # Queue the normal Inbox refresh while acceptance is in flight.
            page.evaluate("htmx.trigger(document.body, 'tasks-changed')")
            page.wait_for_timeout(100)
            pw.expect(editor.locator('[name="disk_order"]')).to_have_value(before)
            route.fulfill(response=response)

        page.route(f"**/confirm/{aid}/accept", poll_during_accept)
        for dismissal in ("Close confirmation", "Back", "Escape", "backdrop"):
            accept.click()
            dialog = page.locator("#confirmation-modal dialog")
            pw.expect(dialog).to_be_visible()
            pw.expect(
                dialog.get_by_role("region", name="Tracks unassigned").locator("li")
            ).to_have_count(1)
            if dismissal == "Escape":
                page.keyboard.press("Escape")
            elif dismissal == "backdrop":
                page.mouse.click(5, 5)
            else:
                dialog.get_by_role("button", name=dismissal, exact=True).click()
            pw.expect(dialog).not_to_be_visible()
            pw.expect(accept).to_be_enabled()
            pw.expect(accept).to_have_css("opacity", "1")
            pw.expect(editor.locator('[name="disk_order"]')).to_have_value(before)
        accept.click()
        pw.expect(dialog).to_be_visible()
        browser.close()


def test_album_assignment_editor_cancels_inline_without_artwork(reset_demo_server):
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.goto(f"{reset_demo_server}/album/demo-rel-dingoes")
        tracks = page.locator("#album-tracks")
        edit = tracks.get_by_role("button", name="Edit track assignments")
        requests = []
        page.on("request", lambda request: requests.append(request.url))
        edit.click()
        editor = tracks.locator(".assignment-content")
        pw.expect(editor).to_be_visible()
        pw.expect(page.get_by_role("dialog")).not_to_be_visible()
        pw.expect(tracks.locator(".tracklist")).not_to_be_visible()
        original = editor.locator('[name="disk_order"]').input_value()
        editor.get_by_role("button", name="Move on-disk entry down", exact=True).first.click()
        pw.expect(editor.locator('[name="disk_order"]')).not_to_have_value(original)
        draft = editor.locator('[name="disk_order"]').input_value()
        # A late comparison must not replace an active draft.
        with page.expect_response(lambda r: "/compare" in r.url):
            page.evaluate("""htmx.ajax('GET', '/library/demo-rel-dingoes/compare',
                {target: '#compare-demo-rel-dingoes', swap: 'innerHTML'})""")
        pw.expect(editor.locator('[name="disk_order"]')).to_have_value(draft)
        editor.get_by_role("button", name="Reset", exact=True).click()
        pw.expect(editor.locator('[name="disk_order"]')).to_have_value(original)
        editor.get_by_role("button", name="Cancel", exact=True).click()
        pw.expect(tracks.locator(".tracklist")).to_be_visible()
        pw.expect(editor).to_have_count(0)
        pw.expect(edit).to_be_enabled()
        pw.expect(edit).to_have_css("opacity", "1")
        pw.expect(edit).to_be_focused()
        edit.click()
        pw.expect(editor.locator('[name="disk_order"]')).to_have_value(original)
        # Also cancel without a comparison replacing the original trigger.
        editor.get_by_role("button", name="Cancel", exact=True).click()
        pw.expect(edit).to_be_enabled()
        pw.expect(edit).to_have_css("opacity", "1")
        pw.expect(edit).to_be_focused()
        edit.click()
        pw.expect(editor.locator('[name="disk_order"]')).to_have_value(original)
        pw.expect(editor.get_by_role("checkbox", name="Use artwork")).to_have_count(0)
        assert not any("/assignments/" in url and "/artwork" in url for url in requests)
        editor.get_by_role("button", name="Move on-disk entry down", exact=True).first.click()
        with page.expect_response(
            lambda r: r.url.endswith("/confirm/demo-rel-dingoes/accept")
        ) as applied:
            editor.get_by_role("button", name="Accept changes").click()
        assert parse_qs(applied.value.request.post_data)["include_artwork"] == ["false"]
        pw.expect(edit).to_be_visible()
        pw.expect(tracks.locator(".tracklist")).to_be_visible()
        browser.close()


def test_unassigned_files_are_proposed_and_remain_editable_until_acceptance(reset_demo_server):
    """The real editor labels proposals and lets the user alter them before writing."""
    from bs4 import BeautifulSoup

    base = reset_demo_server
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        card = _card(page, base)
        aid = card.get_attribute("id").removeprefix("task-")
        # Seed a release-only confirmation through the real guarded API. The
        # three files retain their numbers and receive no track identities.
        initial = page.request.get(f"{base}/confirm/{aid}/preview?release_only=true")
        fields = {
            item["name"]: item.get("value", "")
            for item in BeautifulSoup(initial.text(), "html.parser").select(
                "input[type=hidden][name]"
            )
        }
        accepted = page.request.post(
            f"{base}/confirm/{aid}", headers={"HX-Request": "true"}, form=fields
        )
        assert accepted.ok
        page.goto(f"{base}/album/{fields['candidate_mbid']}")
        page.get_by_role("button", name="Review assignments", exact=True).click()
        editor = page.locator("#album-track-editor")
        pw.expect(editor.locator("[data-proposed-pair]")).to_have_count(3)
        pw.expect(editor.locator("[name=disk_order]")).to_have_value("0,1,2")
        editor.get_by_role("button", name="Move on-disk entry down", exact=True).first.click()
        pw.expect(editor.locator("[name=disk_order]")).to_have_value("1,0,2")
        pw.expect(editor.locator("[data-proposed-pair]")).to_have_count(3)
        editor.get_by_role("button", name="Cancel", exact=True).click()
        page.get_by_role("button", name="Review assignments", exact=True).click()
        pw.expect(editor.locator("[name=disk_order]")).to_have_value("0,1,2")
        editor.get_by_role("button", name="Accept changes").click()
        pw.expect(page.locator("#album-tracks .tracklist")).to_be_visible()
        pw.expect(page.get_by_role("button", name="Review assignments", exact=True)).to_have_count(
            0
        )
        page.get_by_role("button", name="Edit track assignments", exact=True).click()
        pw.expect(editor.locator("[data-proposed-pair]")).to_have_count(0)
        browser.close()
