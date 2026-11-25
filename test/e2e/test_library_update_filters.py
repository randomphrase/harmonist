"""Update menus overlay the grid and keep their selection across swaps (#368)."""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_update_menu_survives_navigation(browser_factory, demo_server, htmx_ready, width, engine):
    browser = browser_factory(engine)
    page = browser.new_page(viewport={"width": width, "height": 900})
    # Opening an album performs the real dry run and populates the queue.
    page.goto(f"{demo_server}/album/demo-rel-rural-juror")
    pw.expect(page.locator("#album-update-demo-rel-rural-juror .sev--structure")).to_be_visible()
    page.get_by_role("link", name="← Back to library", exact=True).click()
    updates = page.get_by_role("navigation", name="Tag update filters", exact=True)
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
    pw.expect(trigger).to_contain_text("Tag updates: Structure")
    trigger.click()
    pw.expect(updates.locator('[aria-current="true"]')).to_contain_text("Structure")
    pw.expect(updates.locator('[aria-disabled="true"]', has_text="Cosmetic")).to_be_visible()
    trigger.press("Escape")

    # Searching and a background refresh both replace the controls. Neither
    # should reset the selected category or reopen the overlay.
    page.get_by_role("searchbox", name="Search the library").fill("Rural")
    htmx_ready(page.get_by_role("button", name="Search", exact=True)).click()
    pw.expect(page).to_have_url(re.compile(r"filter=update-structure&q=Rural"))
    # The URL moves before the swap settles. Wait for the search's new view,
    # rather than a control whose previous copy could still be on screen.
    htmx_ready(page.locator('[data-library-refresh][hx-get*="q=Rural"]')).wait_for(state="attached")
    htmx_ready(trigger).wait_for(state="visible")
    # A status poll may already have a refresh in flight. HTMX drops a second
    # refresh, so observe the replacement even if its response headers arrived
    # before this point. The completed search cannot satisfy this marker.
    library = page.locator("#library-page")
    library.evaluate("""element => {
        element.dataset.beforeRefresh = 'yes';
        document.body.dispatchEvent(new Event('library-refresh'));
    }""")
    pw.expect(library).not_to_have_attribute("data-before-refresh", "yes")
    htmx_ready(trigger).wait_for(state="visible")
    pw.expect(trigger).to_contain_text("Tag updates: Structure")
    pw.expect(updates).to_be_hidden()
    assert page.evaluate("window.__harmonistNoReload === true")

    page.locator("#lib-demo-rel-rural-juror").click()
    page.get_by_role("link", name="← Back to library", exact=True).click()
    pw.expect(page).to_have_url(re.compile(r"filter=update-structure&q=Rural"))
    page.reload()
    pw.expect(trigger).to_contain_text("Tag updates: Structure")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    trigger.click()
    htmx_ready(updates.get_by_role("link", name=re.compile(r"^All"))).click()
    pw.expect(page).to_have_url(re.compile(r"filter=update-available&q=Rural"))
    page.get_by_role("button", name="Clear", exact=True).click()
    pw.expect(updates).to_be_hidden()
    pw.expect(trigger).to_contain_text("Tag updates")
    pw.expect(page).not_to_have_url(re.compile(r"filter="))
    page.context.close()


@pytest.mark.parametrize(
    "navigation_first", [False, True], ids=["refresh-first", "navigation-first"]
)
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_navigation_takes_priority_over_refresh(
    browser_factory, demo_server, htmx_ready, navigation_first, engine
):
    """Hold real responses to force both overlap orders; timing sleeps cannot prove this race."""
    browser = browser_factory(engine)
    page = browser.new_page()
    page.goto(f"{demo_server}/album/demo-rel-rural-juror")
    pw.expect(page.locator("#album-update-demo-rel-rural-juror .sev--structure")).to_be_visible()
    page.get_by_role("link", name="← Back to library", exact=True).click()
    trigger = htmx_ready(page.locator("#library-update-button"))
    trigger.wait_for(state="visible")
    page.evaluate("""() => {
            window.libraryRequests = [];
            document.body.addEventListener('htmx:beforeRequest', e => {
                if (e.detail.target.id !== 'library-rows') return;
                const request = {done: false};
                window.libraryRequests.push(request);
                e.detail.xhr.addEventListener('loadend', () => { request.done = true; });
            });
        }""")
    held = []

    def hold_first(route):
        # Fetch the actual server response, then control its delivery. The
        # other request remains real too, including its history headers.
        if ("filter=update-structure" in route.request.url) == navigation_first:
            held.append((route, route.fetch()))
            page.evaluate("window.libraryResponseHeld = true")
        else:
            route.continue_()

    page.route("**/library?**", hold_first)

    def refresh():
        page.evaluate("document.body.dispatchEvent(new Event('library-refresh'))")

    def navigate():
        trigger.click()
        menu = page.get_by_role("navigation", name="Tag update filters", exact=True)
        htmx_ready(menu.get_by_role("link", name=re.compile("Structure"))).click()

    (navigate if navigation_first else refresh)()
    page.wait_for_function("window.libraryResponseHeld === true")
    (refresh if navigation_first else navigate)()
    if navigation_first:
        # HTMX handles the refresh event synchronously. No second request
        # may start with the old view while navigation is pending.
        assert page.evaluate("window.libraryRequests.length") == 1
    else:
        pw.expect(trigger).to_contain_text("Tag updates: Structure")

    route, response = held.pop()
    route.fulfill(response=response)
    page.wait_for_function("window.libraryRequests.every(request => request.done)")
    pw.expect(trigger).to_contain_text("Tag updates: Structure")
    pw.expect(page).to_have_url(re.compile("filter=update-structure"))
    pw.expect(page.locator("#lib-demo-rel-rural-juror")).to_be_visible()

    # The replacement fragment must still refresh, using the selected view.
    page.unroute("**/library?**", hold_first)
    with page.expect_response(lambda r: "/library?" in r.url and "update-structure" in r.url):
        refresh()
    page.wait_for_function("window.libraryRequests.every(request => request.done)")
    pw.expect(trigger).to_contain_text("Tag updates: Structure")
    pw.expect(page).to_have_url(re.compile("filter=update-structure"))
    page.context.close()
