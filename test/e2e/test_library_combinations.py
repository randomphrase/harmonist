"""Real controls compose queries, clear independently and survive navigation."""

import os
import re
from urllib.parse import parse_qs, urlsplit

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize(("engine", "width"), [("chromium", 1280), ("webkit", 390)])
def test_new_filter(browser_factory, library_filters_server, htmx_ready, engine, width):
    browser = browser_factory(engine)
    page = browser.new_page(viewport={"width": width, "height": 900})
    page.goto(f"{library_filters_server}/?tab=library&limit=1")
    search_box = page.get_by_role("button", name="Search", exact=True).bounding_box()
    clear_box = page.get_by_role("button", name="Clear", exact=True).bounding_box()
    assert abs(search_box["x"] - clear_box["x"]) < 1
    assert abs(search_box["width"] - clear_box["width"]) < 1
    assert clear_box["y"] > search_box["y"] + search_box["height"]
    new = page.get_by_role("button", name="New", exact=True)
    pw.expect(new).to_contain_text("2")
    htmx_ready(page.get_by_role("link", name="Next →", exact=True)).click()
    htmx_ready(new).press("Space")
    pw.expect(new).to_have_attribute("aria-pressed", "true")
    pw.expect(page.locator("#library-page")).to_have_attribute("data-page", "1")
    pw.expect(page.locator("#library-page")).to_have_attribute("data-total-shown", "2")
    htmx_ready(page.get_by_role("link", name="Next →", exact=True)).click()
    pw.expect(page.locator("#library-page")).to_have_attribute("data-page", "2")
    htmx_ready(page.locator('#library-grid a[id^="lib-"]')).click()
    page.get_by_role("link", name="← Back to library", exact=True).click()
    pw.expect(new).to_have_attribute("aria-pressed", "true")
    pw.expect(page.locator("#library-page")).to_have_attribute("data-page", "2")
    page.reload()
    pw.expect(new).to_have_attribute("aria-pressed", "true")
    htmx_ready(page.locator("#library-artwork-button")).click()
    htmx_ready(page.locator("#library-filter-no-artwork")).click()
    pw.expect(page.locator("#library-page")).to_have_attribute("data-total-shown", "1")
    pw.expect(page.locator("#lib-demo-rel-rural-juror")).to_be_visible()
    assert parse_qs(urlsplit(page.url).query)["filter"] == ["new,no-artwork"]
    htmx_ready(new).wait_for(state="visible")
    with page.expect_response(lambda r: "/library?" in r.url):
        page.evaluate("document.body.dispatchEvent(new Event('library-refresh'))")
    pw.expect(new).to_have_attribute("aria-pressed", "true")
    page.get_by_role("searchbox").fill("no-such-album")
    htmx_ready(page.get_by_role("button", name="Search", exact=True)).click()
    pw.expect(page.locator("#library-page")).to_have_attribute("data-total-shown", "0")
    pw.expect(new).to_be_enabled()
    htmx_ready(new).click()
    pw.expect(page).to_have_url(re.compile(r"filter=no-artwork(?:&|$)"))
    assert parse_qs(urlsplit(page.url).query)["filter"] == ["no-artwork"]
    assert parse_qs(urlsplit(page.url).query)["q"] == ["no-such-album"]
    page.get_by_role("button", name="Clear", exact=True).click()
    pw.expect(new).to_have_attribute("aria-pressed", "false")
    pw.expect(page.locator("#library-page")).to_have_attribute("data-total-shown", "9")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.context.close()


@pytest.mark.parametrize(("engine", "width"), [("chromium", 1280), ("webkit", 390)])
def test_combined_filters(browser_factory, library_filters_server, htmx_ready, engine, width):
    browser = browser_factory(engine)
    page = browser.new_page(viewport={"width": width, "height": 900})
    page.goto(f"{library_filters_server}/?tab=library&limit=1")
    formats = page.get_by_role("navigation", name="Format filters", exact=True)
    origins = page.get_by_role("navigation", name="Origin filters", exact=True)
    trigger = page.locator("#library-format-button")
    page.evaluate("window.filtersNoReload = true")

    htmx_ready(trigger).press("Enter")
    pw.expect(formats).to_be_visible()
    box = formats.bounding_box()
    assert 0 <= box["x"] and box["x"] + box["width"] <= width
    trigger.press("Escape")
    pw.expect(trigger).to_be_focused()
    trigger.click()
    htmx_ready(formats.get_by_role("link", name=re.compile(r"^MP3"))).click()
    pw.expect(trigger).to_contain_text("Format: MP3")
    # Paging has the codec, and a new restriction restarts on page one.
    htmx_ready(page.get_by_role("link", name="Next →", exact=True)).click()
    pw.expect(page.locator("#library-page")).to_have_attribute("data-page", "2")
    htmx_ready(page.locator("#library-artwork-button")).press("Space")
    htmx_ready(page.locator("#library-filter-no-artwork")).press("Enter")
    pw.expect(page.locator("#library-page")).to_have_attribute("data-page", "1")
    no_art = page.locator("#library-artwork-button")
    pw.expect(no_art).to_have_text(re.compile(r"^\s*No artwork\s+1\s*$"))
    pw.expect(page.locator('#library-grid a[id^="lib-"]')).to_have_count(1)
    pw.expect(page.locator("#lib-demo-rel-rural-juror")).to_be_visible()

    htmx_ready(page.locator("#library-origin-button")).click()
    htmx_ready(origins.get_by_role("link", name=re.compile(r"^Unknown"))).click()
    # Wait for the choice to land: a menu opened before then is replaced by
    # its response, and its options do not yet carry the new filter (#832).
    pw.expect(page.locator("#library-origin-button")).to_contain_text("Origin: Unknown")
    htmx_ready(trigger).click()
    htmx_ready(formats.get_by_role("button", name="Mixed formats only", exact=True)).click()
    pw.expect(trigger).to_contain_text("Format: MP3 · Mixed")
    selection = "origin-unknown,no-artwork,mixed-format,codec-mp3"
    assert parse_qs(urlsplit(page.url).query)["filter"] == [selection]
    assert page.evaluate("window.filtersNoReload === true")

    page.get_by_role("searchbox").fill("Rural")
    htmx_ready(page.get_by_role("button", name="Search", exact=True)).click()
    pw.expect(page).to_have_url(re.compile("q=Rural"))
    htmx_ready(page.get_by_label("Show", exact=True)).select_option("40")
    pw.expect(page.locator("#library-page")).to_have_attribute("data-limit", "40")
    htmx_ready(trigger).wait_for(state="visible")
    with page.expect_response(lambda r: "/library?" in r.url):
        page.evaluate("document.body.dispatchEvent(new Event('library-refresh'))")
    pw.expect(trigger).to_contain_text("Format: MP3 · Mixed")
    htmx_ready(no_art).click()
    htmx_ready(page.get_by_role("link", name="Any artwork status", exact=True)).click()
    pw.expect(no_art).to_have_text("Artwork")
    page.go_back()
    pw.expect(no_art).to_have_text(re.compile(r"^\s*No artwork\s+1\s*$"))
    page.reload()
    pw.expect(trigger).to_contain_text("Format: MP3 · Mixed")
    htmx_ready(page.locator("#lib-demo-rel-rural-juror")).click()
    page.get_by_role("link", name="← Back to library", exact=True).click()
    pw.expect(no_art).to_have_text(re.compile(r"^\s*No artwork\s+1\s*$"))
    assert parse_qs(urlsplit(page.url).query)["filter"] == [selection]

    # Removing a category preserves all the independent constraints.
    htmx_ready(page.locator("#library-origin-button")).click()
    htmx_ready(origins.get_by_role("link", name="Any origin", exact=True)).click()
    pw.expect(page.locator("#library-origin-button")).to_have_text("Origin")
    htmx_ready(trigger).click()
    htmx_ready(formats.get_by_role("link", name="Any codec", exact=True)).click()
    pw.expect(trigger).to_contain_text("Format: Mixed")
    pw.expect(no_art).to_have_text(re.compile(r"^\s*No artwork\s+1\s*$"))

    # Active controls can always be removed, even with an empty search.
    page.get_by_role("searchbox").fill("no-such-album")
    htmx_ready(page.get_by_role("button", name="Search", exact=True)).click()
    pw.expect(page.locator("#library-page")).to_have_attribute("data-total-shown", "0")
    htmx_ready(no_art).click()
    htmx_ready(page.get_by_role("link", name="Any artwork status", exact=True)).click()
    pw.expect(page).to_have_url(re.compile("filter=mixed-format"))
    htmx_ready(trigger).click()
    htmx_ready(formats.get_by_role("button", name="Mixed formats only", exact=True)).click()
    pw.expect(trigger).to_have_text("Format")
    assert "filter" not in parse_qs(urlsplit(page.url).query)
    assert parse_qs(urlsplit(page.url).query)["q"] == ["no-such-album"]
    page.go_back()
    pw.expect(trigger).to_contain_text("Format: Mixed")
    pw.expect(page.get_by_role("searchbox")).to_have_value("no-such-album")
    clear = page.get_by_role("button", name="Clear", exact=True)
    clear.press("Enter")
    pw.expect(trigger).to_have_text("Format")
    assert "filter" not in parse_qs(urlsplit(page.url).query)
    assert "q" not in parse_qs(urlsplit(page.url).query)
    pw.expect(page.get_by_role("searchbox")).to_have_value("")
    pw.expect(page.locator("#library-page")).to_have_attribute("data-limit", "40")
    # Clear also resets an unsubmitted draft, even without active filters.
    page.get_by_role("searchbox").fill("unfinished search")
    clear.click()
    pw.expect(page.get_by_role("searchbox")).to_have_value("")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.context.close()
