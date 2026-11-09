"""The disc ID search's triggers, which only a browser can see (#984)."""

import os

import pytest
from test.e2e.conftest import review_dialog

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
playwright_sync = pytest.importorskip("playwright.sync_api")

ALBUM = "disc-id-demo"


def _is_lookup(response):
    return response.url.endswith("/disc-id")


def _is_name_search(response):
    return response.url.endswith("/search")


def _radio(page, name):
    return page.get_by_role("radio", name=name, exact=True)


@pytest.mark.parametrize("disc_id_server", ["1"], indirect=True)
def test_the_album_looks_itself_up_and_a_row_opens_review(browser_factory, disc_id_server):
    server, root, _ = disc_id_server
    from harmonist import sidecar

    folder = root / "LCD Soundsystem" / "American Dream"
    before = (folder / "01 Oh Baby.m4a").read_bytes()
    page = browser_factory("chromium").new_page()
    with page.expect_response(_is_lookup):
        page.goto(f"{server}/album/{ALBUM}")
    playwright_sync.expect(_radio(page, "By disc ID")).to_be_checked()
    results = page.locator(f"#mbid-results-{ALBUM}")
    row = results.get_by_role("listitem").first
    playwright_sync.expect(row).to_contain_text("Disc ID matches")
    assert sidecar.read(folder).mb_match_candidate is None
    with page.expect_response(lambda r: "/review/" in r.url):
        row.get_by_role("button", name="Review", exact=True).click()
    review = review_dialog(page)
    playwright_sync.expect(review.get_by_role("heading", name="Suggested match:")).to_be_visible()
    assert sidecar.read(folder).mb_release_id is None
    assert (folder / "01 Oh Baby.m4a").read_bytes() == before
    page.context.close()


@pytest.mark.parametrize("disc_id_server", ["0"], indirect=True)
def test_an_automatic_miss_moves_on_to_a_name_search(browser_factory, disc_id_server):
    server, _, _ = disc_id_server
    page = browser_factory("chromium").new_page()
    lookups = []
    page.on("request", lambda r: lookups.append(r.url) if r.url.endswith("/disc-id") else None)
    with page.expect_response(_is_name_search) as searched:
        page.goto(f"{server}/album/{ALBUM}")
    assert searched.value.ok
    playwright_sync.expect(_radio(page, "By name")).to_be_checked()
    playwright_sync.expect(page.get_by_placeholder("Artist", exact=True)).to_be_visible()
    assert len(lookups) == 1
    page.context.close()


@pytest.mark.parametrize("disc_id_server", ["0"], indirect=True)
def test_a_miss_the_user_asked_for_stays_on_screen(browser_factory, disc_id_server, htmx_ready):
    server, _, _ = disc_id_server
    page = browser_factory("chromium").new_page()
    with page.expect_response(_is_name_search):
        page.goto(f"{server}/album/{ALBUM}")
    playwright_sync.expect(_radio(page, "By name")).to_be_checked()
    results = page.locator(f"#mbid-results-{ALBUM}")

    with page.expect_response(_is_lookup):
        _radio(page, "By disc ID").check()
    playwright_sync.expect(results.get_by_text("No matches.", exact=True)).to_be_visible()
    playwright_sync.expect(_radio(page, "By disc ID")).to_be_checked()

    # The panel's own Search reaches HTMX too, and is equally the user's request.
    with page.expect_response(_is_lookup):
        htmx_ready(page.locator(".find-mode__panel--discid button[type=submit]")).click()
    playwright_sync.expect(results.get_by_text("No matches.", exact=True)).to_be_visible()
    playwright_sync.expect(_radio(page, "By disc ID")).to_be_checked()
    page.context.close()


@pytest.mark.parametrize("disc_id_server", ["error"], indirect=True)
def test_an_automatic_failure_stays_visible(browser_factory, disc_id_server):
    server, _, _ = disc_id_server
    page = browser_factory("chromium").new_page()
    with page.expect_response(_is_lookup):
        page.goto(f"{server}/album/{ALBUM}")
    results = page.locator(f"#mbid-results-{ALBUM}")
    playwright_sync.expect(
        results.get_by_text("Disc ID lookup failed", exact=False)
    ).to_be_visible()
    playwright_sync.expect(_radio(page, "By disc ID")).to_be_checked()
    page.context.close()
