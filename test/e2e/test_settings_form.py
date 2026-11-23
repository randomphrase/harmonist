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


def test_settings_save_round_trips_both_tagging_dropdowns(
    browser_factory, demo_server: str
) -> None:
    browser = browser_factory("chromium")
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
    page.context.close()


def test_the_artist_name_examples_follow_the_controls_and_save(
    browser_factory, demo_server: str
) -> None:
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
    browser = browser_factory("chromium")
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
    page.context.close()


def test_settings_setup_and_restore_preserve_unsaved_preferences(
    browser_factory, public_demo_server: tuple[str, Path], htmx_ready
) -> None:
    base, music = public_demo_server
    # Seed two skipped purchases, so the first restore swaps a populated list.
    (music / "ignores.txt").write_text("12001 # First album\n12002 # Second album\n")
    browser = browser_factory("chromium")
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
    page.context.close()


_LEVELS = ["structure", "identity", "enrichment", "cosmetic", "settings"]


def test_the_tag_update_grid_keeps_both_columns_contiguous_and_saves(
    browser_factory, demo_server: str
) -> None:
    """#273. The tick grid's ranges are kept by a script and its outcome column by
    CSS, both on unsaved clicks, which no route test can see. Clicking a cell
    extends its column to that level or shrinks it to just short of it, and
    extending one column pushes the other back."""
    browser = browser_factory("chromium")
    page = browser.new_page()
    page.set_default_timeout(5000)
    page.goto(f"{demo_server}/settings")

    def cell(column: str, level: str):
        return page.locator(
            f'.update-routing__row[data-level="{level}"] label:has(.update-routing__{column})'
        )

    def state() -> dict[str, str]:
        ticks = {}
        for level in _LEVELS:
            row = page.locator(f'.update-routing__row[data-level="{level}"]')
            apply = row.locator(".update-routing__apply")
            if apply.count() and apply.is_checked():
                ticks[level] = "apply"
            elif row.locator(".update-routing__review").is_checked():
                ticks[level] = "review"
            else:
                ticks[level] = "-"
        return ticks

    def outcomes() -> list[str]:
        return [
            page.locator(f'.update-routing__row[data-level="{level}"] .update-routing__outcome')
            .inner_text()
            .strip()
            for level in _LEVELS
        ]

    assert state() == {"structure": "review", **dict.fromkeys(_LEVELS[1:], "-")}

    cell("apply", "enrichment").click()  # extend Apply up to Enrichment
    assert state() == {
        "structure": "review",
        "identity": "-",
        "enrichment": "apply",
        "cosmetic": "apply",
        "settings": "apply",
    }
    assert outcomes() == [
        "Sent to the Inbox",
        "Left in the Library",
        "Applied automatically",
        "Applied automatically",
        "Applied automatically",
    ]

    cell("review", "enrichment").click()  # Inbox down to Enrichment pushes Apply back
    assert state() == {
        "structure": "review",
        "identity": "review",
        "enrichment": "review",
        "cosmetic": "apply",
        "settings": "apply",
    }

    cell("apply", "settings").click()  # the last Apply tick: shrink to nothing
    assert state() == {
        "structure": "review",
        "identity": "review",
        "enrichment": "review",
        "cosmetic": "-",
        "settings": "-",
    }

    cell("apply", "identity").click()  # Apply up to Identity pushes the Inbox back
    cell("review", "structure").click()  # and unticking Structure empties the Inbox
    assert state() == {
        "structure": "-",
        "identity": "apply",
        "enrichment": "apply",
        "cosmetic": "apply",
        "settings": "apply",
    }
    playwright_sync.expect(
        page.locator('.update-routing__row[data-level="structure"] .update-routing__cell').first
    ).to_have_text("never")

    with page.expect_response(
        lambda r: r.url.endswith("/settings") and r.request.method == "POST"
    ) as saved:
        page.get_by_role("button", name="Save settings", exact=True).click()
    assert saved.value.ok
    page.reload()
    assert state() == {"structure": "-", **dict.fromkeys(_LEVELS[1:], "apply")}

    # Back to the shipped levels, so the module's other tests meet the demo
    # as they expect it.
    cell("apply", "settings").click()
    cell("review", "structure").click()
    with page.expect_response(lambda r: r.url.endswith("/settings") and r.request.method == "POST"):
        page.get_by_role("button", name="Save settings", exact=True).click()
    page.context.close()


def test_the_activity_column_runs_down_from_structure_on_its_own_and_saves(
    browser_factory, demo_server: str
) -> None:
    """#1024. The Activity column is kept contiguous by the same script as the
    other two, but moves neither of them and is moved by neither; and its ticks
    don't count as a destination in the outcome column."""
    browser = browser_factory("chromium")
    page = browser.new_page()
    page.set_default_timeout(5000)
    page.goto(f"{demo_server}/settings")

    def cell(column: str, level: str):
        return page.locator(
            f'.update-routing__row[data-level="{level}"] label:has(.update-routing__{column})'
        )

    def recorded() -> list[str]:
        return [
            level
            for level in _LEVELS[:-1]  # Settings has no box
            if page.locator(
                f'.update-routing__row[data-level="{level}"] .update-routing__activity'
            ).is_checked()
        ]

    def outcome(level: str) -> str:
        return (
            page.locator(f'.update-routing__row[data-level="{level}"] .update-routing__outcome')
            .inner_text()
            .strip()
        )

    assert recorded() == ["structure", "identity", "enrichment", "cosmetic"]
    settings_cells = page.locator(
        '.update-routing__row[data-level="settings"] .update-routing__cell'
    )
    assert settings_cells.nth(2).inner_text() == "never"  # Apply, Inbox, then Activity
    assert outcome("identity") == "Left in the Library"

    cell("activity", "identity").click()  # shrink to just short of Identity
    assert recorded() == ["structure"]

    cell("review", "enrichment").click()  # the Inbox moves; Activity doesn't
    cell("apply", "cosmetic").click()
    assert recorded() == ["structure"]

    cell("activity", "enrichment").click()  # extend down to Enrichment
    assert recorded() == ["structure", "identity", "enrichment"]
    assert page.locator(
        '.update-routing__row[data-level="enrichment"] .update-routing__review'
    ).is_checked()
    assert outcome("cosmetic") == "Applied automatically"

    with page.expect_response(
        lambda r: r.url.endswith("/settings") and r.request.method == "POST"
    ) as saved:
        page.get_by_role("button", name="Save settings", exact=True).click()
    assert saved.value.ok
    page.reload()
    assert recorded() == ["structure", "identity", "enrichment"]

    # Back to the shipped levels for the module's other tests.
    cell("activity", "cosmetic").click()
    cell("apply", "settings").click()
    cell("review", "identity").click()
    assert recorded() == ["structure", "identity", "enrichment", "cosmetic"]
    with page.expect_response(lambda r: r.url.endswith("/settings") and r.request.method == "POST"):
        page.get_by_role("button", name="Save settings", exact=True).click()
    page.context.close()
