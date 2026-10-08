"""Update menus overlay the grid and keep their selection across swaps (#368)."""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_update_menu_survives_navigation(demo_server, htmx_ready, width, engine):
    with pw.sync_playwright() as playwright:
        browser = getattr(playwright, engine).launch()
        page = browser.new_page(viewport={"width": width, "height": 900})
        # Opening an album performs the real dry run and populates the queue.
        page.goto(f"{demo_server}/album/demo-rel-rural-juror")
        pw.expect(
            page.locator("#album-update-demo-rel-rural-juror .sev--structure")
        ).to_be_visible()
        page.get_by_role("link", name="← Back to library", exact=True).click()
        updates = page.get_by_role("navigation", name="Update filters", exact=True)
        trigger = page.locator("#library-update-button")
        pw.expect(updates).to_be_hidden()
        page.evaluate("window.__harmonistNoReload = true")

        def grid_offset():
            # Async status banners above Library can change the page position;
            # the controls must keep the grid's position within Library stable.
            return (
                page.locator("#library-grid").bounding_box()["y"]
                - page.locator("#library-page").bounding_box()["y"]
            )

        grid_y = grid_offset()
        trigger.press("Enter")
        pw.expect(updates).to_be_visible()
        assert grid_offset() == grid_y
        menu_box = updates.bounding_box()
        assert 0 <= menu_box["x"] and menu_box["x"] + menu_box["width"] <= width
        trigger.press("Escape")
        pw.expect(updates).to_be_hidden()
        pw.expect(trigger).to_be_focused()
        trigger.click()
        page.get_by_role("searchbox", name="Search the library").click()
        pw.expect(updates).to_be_hidden()
        trigger.click()
        htmx_ready(updates.get_by_role("link", name=re.compile(r"Structure"))).press("Enter")
        pw.expect(page).to_have_url(re.compile(r"filter=update-structure"))
        assert page.evaluate("window.__harmonistNoReload === true")
        pw.expect(updates).to_be_hidden()
        pw.expect(trigger).to_contain_text("Updates: Structure")
        trigger.click()
        pw.expect(updates.locator('[aria-current="true"]')).to_contain_text("Structure")
        pw.expect(updates.locator('[aria-disabled="true"]', has_text="Cosmetic")).to_be_visible()
        trigger.press("Escape")

        # Searching and a background refresh both replace the controls. Neither
        # should reset the selected category or reopen the overlay.
        page.get_by_role("searchbox", name="Search the library").fill("Rural")
        htmx_ready(page.get_by_role("button", name="Search", exact=True)).click()
        pw.expect(page).to_have_url(re.compile(r"filter=update-structure&q=Rural"))
        htmx_ready(trigger).wait_for(state="visible")
        with page.expect_response(lambda r: "/library?" in r.url and "update-structure" in r.url):
            page.evaluate("document.body.dispatchEvent(new Event('library-refresh'))")
        pw.expect(trigger).to_contain_text("Updates: Structure")
        pw.expect(updates).to_be_hidden()
        assert page.evaluate("window.__harmonistNoReload === true")

        page.locator("#lib-demo-rel-rural-juror").click()
        page.get_by_role("link", name="← Back to library", exact=True).click()
        pw.expect(page).to_have_url(re.compile(r"filter=update-structure&q=Rural"))
        page.reload()
        pw.expect(trigger).to_contain_text("Updates: Structure")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

        trigger.click()
        htmx_ready(updates.get_by_role("link", name=re.compile(r"^All"))).click()
        pw.expect(page).to_have_url(re.compile(r"filter=update-available&q=Rural"))
        main_filters = page.get_by_role("navigation", name="Library filters", exact=True)
        htmx_ready(main_filters.get_by_role("link", name=re.compile(r"^All"))).click()
        pw.expect(updates).to_be_hidden()
        pw.expect(trigger).to_contain_text("Update available")
        pw.expect(page).not_to_have_url(re.compile(r"filter="))
        browser.close()
