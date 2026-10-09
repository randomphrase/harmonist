"""Settings controls must submit and survive reload through the real browser.

The title dropdown and the Save button's form association are invisible to
route-only tests, which construct their own POST instead of using the controls.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1", reason="e2e disabled (set RUN_E2E=1)"
)
playwright_sync = pytest.importorskip("playwright.sync_api")


def test_settings_save_round_trips_both_tagging_dropdowns(demo_server: str) -> None:
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(5000)
        page.goto(f"{demo_server}/settings")
        title = page.get_by_label("Album title", exact=True)
        cover = page.get_by_label("Create cover.jpg in album folders")
        written = page.locator(".settings-title td:last-child span:visible")
        expected = {
            "album_disambiguation": [
                "Fever Dog (live)",
                "Paper Moons (Japanese Release)",
                "Paper Moons (2025 Remaster)",
            ],
            "album_disambiguation_if_needed": [
                "Fever Dog",
                "Paper Moons (Japanese Release)",
                "Paper Moons (2025 Remaster)",
            ],
            "": ["Fever Dog", "Paper Moons", "Paper Moons"],
        }
        for title_value, cover_value in [
            ("album_disambiguation", "if_missing"),
            ("album_disambiguation_if_needed", "never"),
            ("", "never"),
        ]:
            title.select_option(title_value)
            cover.select_option(cover_value)
            playwright_sync.expect(written).to_have_text(expected[title_value])
            if not title_value:
                # The saved transform is on: choosing Title only must retain
                # main's warning about pending removal updates (#685).
                warning = page.locator(".transform-off-warning")
                playwright_sync.expect(warning).to_be_visible()
                title.select_option("album_disambiguation")
                playwright_sync.expect(warning).to_be_hidden()
                title.select_option("")
                playwright_sync.expect(warning).to_be_visible()
            with page.expect_response(
                lambda r: r.url.endswith("/settings") and r.request.method == "POST"
            ) as saved:
                page.get_by_role("button", name="Save settings", exact=True).click()
            assert saved.value.ok
            playwright_sync.expect(page.get_by_text("Settings saved", exact=True)).to_be_visible()
            page.reload()
            playwright_sync.expect(title).to_have_value(title_value)
            playwright_sync.expect(cover).to_have_value(cover_value)
            playwright_sync.expect(written).to_have_text(expected[title_value])
        browser.close()


def test_the_artist_name_examples_follow_the_controls_and_save(demo_server: str) -> None:
    """#678. The written-as column is CSS following the select and the checkbox,
    unsaved changes included — which no route test can see — and the choice
    must survive a save and reload."""
    expected = {
        # choice, list box ticked -> what the written-as cells show
        ("none", False): ["Florence and the Machine", "Mos Def", "Mos Def"],
        ("variations", False): ["Florence + the Machine", "Mos Def", "Mos Def"],
        ("variations", True): ["Florence + the Machine", "Mos Def", "Yasiin Bey"],
        ("all", False): ["Florence + the Machine", "Yasiin Bey", "Yasiin Bey"],
    }
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(5000)
        page.goto(f"{demo_server}/settings")
        names = page.get_by_label("Standardize artist names", exact=True)
        lists = page.get_by_label("Always standardize multi-valued artist tags")
        written = page.locator(".settings-artists .settings-example td + td span:visible")
        for (choice, ticked), cells in expected.items():
            names.select_option(choice)
            lists.set_checked(ticked)
            playwright_sync.expect(written).to_have_text(cells)

        names.select_option("all")
        lists.set_checked(True)
        with page.expect_response(
            lambda r: r.url.endswith("/settings") and r.request.method == "POST"
        ) as saved:
            page.get_by_role("button", name="Save settings", exact=True).click()
        assert saved.value.ok
        page.reload()
        playwright_sync.expect(names).to_have_value("all")
        playwright_sync.expect(lists).to_be_checked()
        browser.close()


def test_settings_setup_and_restore_preserve_unsaved_preferences(
    public_demo_server: tuple[str, Path], htmx_ready
) -> None:
    base, music = public_demo_server
    # Seed two skipped purchases, so the first restore swaps a populated list.
    (music / "ignores.txt").write_text("12001 # First album\n12002 # Second album\n")
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(5000)
        page.goto(f"{base}/settings")
        user_agent = page.get_by_label("MusicBrainz user agent", exact=True)
        default_agent = user_agent.input_value()
        hint = page.locator("#user-agent-hint")
        playwright_sync.expect(hint).to_be_visible()
        user_agent.fill("Harmonist/1.0 ( listener@example.com )")
        playwright_sync.expect(hint).to_be_hidden()
        user_agent.fill(default_agent)
        playwright_sync.expect(hint).to_be_visible()
        page.get_by_label("Max downloads per sync").fill("23")

        requests: list[tuple[str, str]] = []
        page.on("request", lambda r: requests.append((r.method, r.url)))
        setup = page.locator('main button[hx-get="/bandcamp/setup"]')
        htmx_ready(setup).click()
        playwright_sync.expect(page.locator("#modal dialog")).to_be_visible()
        page.keyboard.press("Escape")
        playwright_sync.expect(page.locator("#modal dialog")).to_have_count(0)

        ignored = page.locator("#ignored-section details")
        playwright_sync.expect(ignored.locator("ul")).to_be_hidden()
        ignored.locator("summary").click()
        with page.expect_response(lambda r: r.url.endswith("/ignored/12001/restore")):
            htmx_ready(page.get_by_role("button", name="Restore First album", exact=True)).click()
        playwright_sync.expect(
            page.get_by_role("button", name="Restore Second album", exact=True)
        ).to_be_visible()
        playwright_sync.expect(page.get_by_label("Max downloads per sync")).to_have_value("23")
        assert ("POST", f"{base}/settings") not in requests
        assert ("POST", f"{base}/ignored/12001/restore") in requests
        assert ("GET", f"{base}/bandcamp/setup") in requests
        browser.close()
