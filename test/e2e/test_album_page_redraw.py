"""An open album page shows what a background pass wrote into it (#975).

The auto-apply pass, a sync or a reconcile can write into the album a page is
showing. The page hears that a scan published something through the status
bar's `tasks-changed`, asks whether it was this album, and redraws, unless a
choice is in progress there, when it says so instead. Its own writes it has
drawn already. All of this is when a request happens and what a page does with
it, which only a browser can show.
"""

from __future__ import annotations

import json
import os
import urllib.request

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")

# The demo album seeded with `"art": "gaps"`: one track carries no image, so
# the Artwork section offers to add it, and a background pass can.
ALBUM_ID = "demo-rel-dingoes"
CHANGED = "This album has changed"
APPLY = "Apply this album's artwork"


def _background_write(base: str) -> None:
    request = urllib.request.Request(
        f"{base}/test/background-write/{ALBUM_ID}",
        method="POST",
        headers={"HX-Request": "true"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        assert json.load(response) == {"written": 1}


def _open(page, base: str):
    """The album page with its Artwork section drawn, and a mark on the
    document that a reload would take away. Returns the Apply artwork button."""
    page.goto(f"{base}/album/{ALBUM_ID}")
    apply = page.locator("#album-artwork").get_by_role("button", name=APPLY)
    pw.expect(apply).to_be_visible()
    page.evaluate("window.drawnBeforeTheWrite = true")
    return apply


def test_an_open_album_page_redraws_when_a_background_pass_writes_into_it(
    browser_factory, background_write_server
):
    page = browser_factory("chromium").new_page()
    apply = _open(page, background_write_server)

    with page.expect_navigation():
        _background_write(background_write_server)

    pw.expect(page.locator("#album-artwork .art-row").first).to_be_visible()
    assert page.evaluate("window.drawnBeforeTheWrite") is None
    pw.expect(apply).to_have_count(0)
    pw.expect(page.locator("#harmonist-status")).not_to_contain_text(CHANGED)


def test_a_page_with_a_choice_open_says_the_album_changed_instead(
    browser_factory, background_write_server
):
    """A redraw would throw the choice away, so the page keeps it and offers
    the redraw, which happens only when asked for."""
    page = browser_factory("chromium").new_page()
    apply = _open(page, background_write_server)
    section = page.locator("#album-artwork")
    with page.expect_response(lambda r: "/artwork?" in r.url):
        section.get_by_role(
            "button", name="Keep the current artwork for", exact=False
        ).first.click()
    pw.expect(apply).to_have_count(0)
    assert section.locator('input[name="use"]').first.get_attribute("value")

    _background_write(background_write_server)

    status = page.locator("#harmonist-status")
    pw.expect(status).to_contain_text(CHANGED)
    assert page.evaluate("window.drawnBeforeTheWrite") is True
    assert section.locator('input[name="use"]').first.get_attribute("value")

    with page.expect_navigation():
        status.get_by_role("button", name="Reload").click()
    assert page.evaluate("window.drawnBeforeTheWrite") is None
    pw.expect(page.locator("#album-artwork .art-row").first).to_be_visible()
    pw.expect(page.locator("#album-artwork").get_by_role("button", name=APPLY)).to_have_count(0)


@pytest.mark.parametrize("announced", [True, False], ids=["announced", "quiet"])
def test_the_pages_own_write_does_not_redraw_it(
    browser_factory, background_write_server, announced
):
    """“Better artwork wanted” writes the album's sidecar and redraws only the
    Artwork section, so the album changes under a page that has drawn the
    change. Its response announces a scan as it lands, which a check then
    overlaps (`announced`); a write announcing nothing is found by a scan
    announced later (`quiet`). Either way the page takes it as its own write.
    (Apply artwork is no case of this: it reloads the page itself.)"""
    page = browser_factory("chromium").new_page()
    if not announced:

        def quiet(route):
            response = route.fetch()
            headers = {k: v for k, v in response.headers.items() if k.lower() != "hx-trigger"}
            route.fulfill(response=response, headers=headers)

        page.route("**/artwork/watch", quiet)
    _open(page, background_write_server)
    album_page = page.locator(".album-page")
    drawn = album_page.get_attribute("data-revision")
    assert drawn

    with page.expect_response(lambda r: r.url.endswith("/artwork/watch")):
        page.get_by_role("checkbox", name="Better artwork wanted").click()
    pw.expect(album_page).not_to_have_attribute("data-revision", drawn)
    pw.expect(page.get_by_role("checkbox", name="Better artwork wanted")).to_be_checked()

    # Raised directly, as the status bar raises it for any scan, then a status
    # poll landing after that check's answer.
    with page.expect_response(lambda r: r.url.endswith("/revision")):
        page.evaluate("document.body.dispatchEvent(new Event('tasks-changed'))")
    with page.expect_response(lambda r: r.url.endswith("/status")):
        pass
    assert page.evaluate("window.drawnBeforeTheWrite") is True
    pw.expect(page.locator("#harmonist-status")).not_to_contain_text(CHANGED)
