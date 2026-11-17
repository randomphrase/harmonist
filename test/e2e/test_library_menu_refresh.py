"""The Library's background refreshes: one keeps an open filter menu open (#823),
and one owed before the page's first status poll still happens (#1013)."""

import os
import re
import urllib.request

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")

ARTLESS = "demo-rel-rural-juror"


def test_an_artwork_change_before_the_first_poll_still_refreshes_the_library(
    browser_factory, library_filters_server
):
    """An artwork check can change what the Library shows between the page being
    drawn and its first status poll. The first poll is held here until a check
    asked of the server directly has found the artless album an Addition, which
    is that order made certain."""
    server = library_filters_server
    page = browser_factory("chromium").new_page()
    held = []
    page.route("**/status", lambda route: held.append(route), times=1)
    with page.expect_request("**/status"):
        page.goto(f"{server}/?tab=library")
    badge = page.locator(f"#lib-{ARTLESS} .sev--artwork")
    pw.expect(page.locator(f"#lib-{ARTLESS}")).to_be_visible()
    pw.expect(badge).to_have_count(0)
    assert held, "the page's first status poll was not held"

    # The check an album page sends, asked of the server: the page's poll is held.
    with urllib.request.urlopen(f"{server}/album/{ARTLESS}/artwork?check=1", timeout=30):
        pass
    with urllib.request.urlopen(f"{server}/library?filter=artwork-addition", timeout=10) as r:
        assert f'id="lib-{ARTLESS}"' in r.read().decode(), "the check found no Addition"

    held.pop().continue_()
    pw.expect(badge).to_contain_text("Addition", timeout=10000)
    page.context.close()


@pytest.mark.parametrize(("engine", "width"), [("chromium", 1280), ("webkit", 390)])
# Artwork's options carry ids; Origin's are plain links, found again by href.
@pytest.mark.parametrize(("key", "choice"), [("artwork", "No artwork"), ("origin", "Unknown")])
def test_background_refresh_keeps_open_menu(
    browser_factory, library_filters_server, htmx_ready, engine, width, key, choice
):
    browser = browser_factory(engine)
    page = browser.new_page(viewport={"width": width, "height": 900})
    page.goto(f"{library_filters_server}/?tab=library")
    trigger = page.locator(f"#library-{key}-button")
    menu = page.locator(f"#library-{key}-menu")
    option = menu.get_by_role("link", name=re.compile(f"^{choice}"))
    htmx_ready(trigger).wait_for(state="visible")

    # A scan finishing, say, sends the refresh before the menu opens and its
    # response lands while it is open: hold the response across the opening.
    held = []
    page.route(re.compile(r"/library\?page="), lambda route: held.append(route), times=1)
    page.evaluate("document.getElementById('library-page').dataset.stale = '1'")
    with page.expect_request(re.compile(r"/library\?page=")):
        page.evaluate("document.body.dispatchEvent(new Event('library-refresh'))")
    trigger.click()
    pw.expect(menu).to_be_visible()
    option.focus()
    assert len(held) == 1
    held.pop().continue_()
    pw.expect(page.locator("#library-page[data-stale]")).to_have_count(0)
    page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")

    pw.expect(menu).to_be_visible()
    pw.expect(option).to_be_focused()
    # A choice made in the menu still closes it.
    htmx_ready(option).click()
    pw.expect(trigger).to_contain_text(choice)
    pw.expect(menu).to_be_hidden()
    page.context.close()
