"""Delayed archive responses show progress without hiding local artwork."""

import os
from urllib.parse import parse_qs, urlsplit

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize(("engine", "width"), [("chromium", 1280), ("webkit", 390)])
def test_archive_loading_failure_and_retry(browser_factory, reset_demo_server, engine, width):
    album = "demo-rel-dingoes"
    browser = browser_factory(engine)
    page = browser.new_page(viewport={"width": width, "height": 1000})
    pending = []

    def hold(route):
        query = parse_qs(urlsplit(route.request.url).query)
        if "check" in query or "reread" in query or "more" in query:
            pending.append(route)
        else:
            route.continue_()

    page.route(f"**/album/{album}/artwork?*", hold)
    page.goto(f"{reset_demo_server}/album/{album}")
    section = page.locator("#album-artwork")
    progress = section.locator(".caa-progress")
    placeholder = section.locator(".art-pick__loading")
    refresh = section.get_by_role(
        "button", name="Ask the Cover Art Archive about this release again"
    )
    pw.expect(progress).to_be_visible()
    pw.expect(progress).to_have_text("In progress")
    pw.expect(placeholder).to_be_visible()
    pw.expect(section.locator(".art-pick__others")).to_be_hidden()
    pw.expect(section.locator(".art-row__art img").first).to_be_visible()
    pw.expect(refresh).to_be_disabled()
    assert len(pending) == 1
    # A failed HTTP request must retire its indicator and leave retry usable.
    pending.pop().fulfill(status=503, body="Unavailable")
    pw.expect(progress).to_be_hidden()
    pw.expect(placeholder).to_be_hidden()
    pw.expect(section.locator("header")).to_contain_text("not yet")
    pw.expect(refresh).to_be_enabled()
    others = section.get_by_role("button", name="Other releases ›")
    others.click()
    pw.expect(placeholder).to_be_visible()
    pw.expect(others).to_be_hidden()
    pending.pop().fulfill(status=503, body="Unavailable")
    pw.expect(placeholder).to_be_hidden()
    pw.expect(others).to_be_visible()
    refresh.click()
    pw.expect(progress).to_be_visible()
    pw.expect(placeholder).to_be_visible()
    pending.pop().continue_()
    pw.expect(section.locator('dd[title^="Cover Art Archive last asked"]')).to_be_visible()
    pw.expect(progress).to_be_hidden()
    # The image the picker shows: the album's own come first, unshown (#1012).
    shown = section.locator(".art-pick__image:visible")
    pw.expect(shown).to_have_count(1)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    # With an image already available, a refresh preserves it while checking.
    refresh.click()
    pw.expect(progress).to_be_visible()
    pw.expect(shown).to_have_count(1)
    pending.pop().fulfill(status=503, body="Unavailable")
    pw.expect(progress).to_be_hidden()
    pw.expect(section.locator('dd[title^="Cover Art Archive last asked"]')).to_be_visible()
    page.context.close()
