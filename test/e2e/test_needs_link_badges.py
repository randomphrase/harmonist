"""Needs Linking badge controls work from both hosts, including keyboard activation."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1", reason="e2e disabled (set RUN_E2E=1)"
)
playwright_sync = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize("on_album", [False, True], ids=["inbox", "album"])
@pytest.mark.parametrize("action", ["rematch", "manual"])
def test_needs_link_badge_action(reset_demo_server: str, on_album: bool, action: str) -> None:
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(reset_demo_server)
        card = page.locator("#task-demo-rel-dingoes")
        card.wait_for(state="visible")
        if on_album:
            card.get_by_role("link", name="Little Bit o' Hoot, Whole Lotta Nanny").click()
            page.wait_for_url("**/album/demo-rel-dingoes")
            host = page.locator("main")
        else:
            host = card
        label = (
            "Wrong MusicBrainz match — pick the correct release"
            if action == "rematch"
            else "Mark purchased elsewhere"
        )
        button = host.get_by_role("button", name=label, exact=True)
        # Each control and its external badge remain one visual group.
        badge = button.locator("..").get_by_role("link")
        expected_url = (
            "https://musicbrainz.org/release/demo-rel-dingoes"
            if action == "rematch"
            else "https://dingoes.bandcamp.com/album/little-bit-o-hoot"
        )
        assert badge.get_attribute("href") == expected_url
        assert badge.get_attribute("target") == "_blank"
        assert button.get_attribute("title")
        dialogs: list[str] = []

        def confirm(dialog):
            dialogs.append(dialog.message)
            dialog.accept()

        page.on("dialog", confirm)
        button.focus()
        with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith(f"/{action}")
        ) as response:
            page.keyboard.press("Enter")
        assert response.value.ok
        assert len(dialogs) == 1
        if action == "rematch":
            page.wait_for_url(reset_demo_server + "/")
            corrected = page.get_by_role(
                "link", name="Little Bit o' Hoot, Whole Lotta Nanny", exact=True
            ).locator("xpath=ancestor::div[starts-with(@id, 'task-')][1]")
            corrected.get_by_role("radio", name="Search by name").check()
            corrected.get_by_role("button", name="Search", exact=True).wait_for()
        elif on_album:
            # Reload must expose the new state and remove the live Needs Linking control.
            playwright_sync.expect(
                page.get_by_role("button", name=label, exact=True)
            ).to_have_count(0)
            page.get_by_role(
                "heading", name="Little Bit o' Hoot, Whole Lotta Nanny — Dingoes Ate My Baby"
            ).wait_for()
        else:
            card.wait_for(state="detached")
        browser.close()
