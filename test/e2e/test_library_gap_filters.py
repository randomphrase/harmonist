"""The Structural gaps menu opens over the grid and keeps a choice across swaps (#1009)."""

import os
import re
from urllib.parse import parse_qs, urlsplit

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize(("engine", "width"), [("chromium", 1280), ("webkit", 390)])
def test_gaps_menu(browser_factory, demo_server, htmx_ready, engine, width):
    browser = browser_factory(engine)
    page = browser.new_page(viewport={"width": width, "height": 900})
    page.goto(f"{demo_server}/?tab=library")
    trigger = page.locator("#library-gaps-button")
    menu = page.get_by_role("navigation", name="Structural gap filters", exact=True)
    closed = re.compile(r"^\s*Structural gaps\s+(\d+)\s*$")
    pw.expect(trigger).to_have_text(closed)
    match = closed.match(trigger.inner_text())
    assert match is not None
    page.evaluate("window.gapsNoReload = true")

    htmx_ready(trigger).press("Enter")
    pw.expect(menu).to_be_visible()
    box = menu.bounding_box()
    assert box is not None
    assert 0 <= box["x"] and box["x"] + box["width"] <= width
    trigger.press("Escape")
    pw.expect(menu).to_be_hidden()
    pw.expect(trigger).to_be_focused()

    trigger.click()
    htmx_ready(menu.get_by_role("link", name=re.compile(r"^Incomplete"))).click()
    pw.expect(page).to_have_url(re.compile(r"filter=incomplete"))
    chosen = re.compile(r"Structural gaps: Incomplete\s+(\d+)")
    pw.expect(trigger).to_have_text(chosen)
    count = chosen.search(trigger.inner_text())
    assert count is not None and int(count[1]) > 0
    pw.expect(page.locator("#library-page")).to_have_attribute("data-total-shown", count[1])
    assert page.evaluate("window.gapsNoReload === true")

    trigger.click()
    pw.expect(menu.locator('[aria-current="true"]')).to_contain_text("Incomplete")
    htmx_ready(menu.get_by_role("link", name="Any structure status", exact=True)).click()
    pw.expect(trigger).to_have_text(re.compile(rf"^\s*Structural gaps\s+{match[1]}\s*$"))
    assert "filter" not in parse_qs(urlsplit(page.url).query)
    assert page.evaluate("window.gapsNoReload === true")
