"""Browser smoke test for modal actions (the layer unit tests can't see).

Issue #40 was invisible to the Python suite: the handler, template, and
route were all individually correct — only a real browser exercises the
onclick/htmx event ordering. This smoke test opens the album detail dialog
in demo mode and verifies that a destructive-ish action button actually
fires its confirm and its request.

Opt-in: requires playwright (`pip install -e .[e2e]` + `playwright install
chromium`) and RUN_E2E=1, so `make test` / `make check` stay green without
a browser. Run via `make e2e`.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1", reason="e2e disabled (set RUN_E2E=1)"
)
playwright_sync = pytest.importorskip("playwright.sync_api")

# The rematch pencil, named by the label it is reachable by rather than by its
# endpoint. `button[hx-post*="/rematch"]` used to be unique on an album page and
# is not any more: the deleted-release banner (#210) offers "Find a new release",
# which posts to the same route with a different confirm — and the demo album
# that opens first is precisely the one that carries it, so the bare selector
# resolved to two elements and every click through it died on strict mode. The
# tests below are about the pencil's confirm/side-effect ordering (#40), so they
# have to say which button they mean.
_PENCIL = 'button[aria-label^="Wrong MusicBrainz match"]'


def test_old_deep_link_lands_on_the_album_page(demo_server: str) -> None:
    """`?album=<id>` opened a dialog before there was an album page (#65). Now it
    redirects there (#103). Those links are written into durable activity
    entries, so this URL keeps arriving indefinitely and must keep working —
    followed as a real navigation, since that's what an <a href> does.
    """
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(demo_server)

        # Discover a real album id the way the UI exposes it (tile → detail).
        page.click('[data-tab="library"]')
        tile = page.locator('#panel-library a[href^="/album/"]').first
        tile.wait_for(state="attached")
        album_id = (tile.get_attribute("href") or "").split("/album/")[1]
        assert album_id

        page.goto(f"{demo_server}/?album={album_id}")

        assert page.url.endswith(f"/album/{album_id}"), f"did not redirect: {page.url}"
        page.locator("text=History").first.wait_for(state="visible")

        browser.close()


def test_missing_album_notice_dismisses_and_clears_the_url(demo_server: str) -> None:
    """#71: the notice must actually go away.

    Both halves are invisible to pytest, which only sees the markup: whether the
    dismiss button really removes the element, and whether `history.replaceState`
    really drops `?album=` (so a refresh doesn't resurrect a message about a
    navigation that already happened).
    """
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(f"{demo_server}/?album=definitely-not-a-real-album-id")

        notice = page.locator("#deep-link-notice")
        notice.wait_for(state="visible")
        # The parameter is gone from the address bar before any interaction...
        assert "album=" not in page.url
        # ...and a reload therefore does not bring the notice back.
        page.reload()
        assert page.locator("#deep-link-notice").count() == 0

        # And the dismiss control removes it outright.
        page.goto(f"{demo_server}/?album=definitely-not-a-real-album-id")
        notice.wait_for(state="visible")
        page.click('#deep-link-notice button[aria-label="Dismiss"]')
        notice.wait_for(state="detached")

        browser.close()


def test_activity_album_link_actually_opens_the_album(demo_server: str) -> None:
    """The full round trip that shipped broken: do an action, then click the
    album link the Activity feed writes for it.

    The Python suite can prove the id is recorded and that `/` resolves it, but
    only this exercises the real chain end to end — action → recorded id →
    rendered link → resolve → dialog. The first cut recorded a pre-mutation id
    that tagging had already erased, so every link landed on the "isn't in your
    library any more" notice.
    """
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(demo_server)

        # An action against a real album, so the feed gets an album-tagged entry.
        # The tile is a link to the album's page now, not a dialog trigger (#129).
        page.click('[data-tab="library"]')
        page.locator('#panel-library a[href^="/album/"]').first.click()
        page.locator(_PENCIL).wait_for(state="visible")
        page.on("dialog", lambda d: d.accept())
        with page.expect_request(lambda r: "/rematch" in r.url and r.method == "POST"):
            page.click(_PENCIL)
        page.wait_for_url(f"{demo_server}/")  # rematch sends it out of the Library

        # Its Activity entry must carry a link on the album name.
        page.click('[data-tab="activity"]')
        link = page.locator('#panel-activity a[href^="/album/"]').first
        link.wait_for(state="visible")
        link.click()

        # Following it lands on the album's page (#103) with its history —
        # NOT the unresolvable-album notice.
        page.locator("text=History").first.wait_for(state="visible")
        assert "/album/" in page.url
        assert "isn't in your library any more" not in page.content()

        browser.close()


def test_album_page_rematch_fires_confirm_and_post_then_navigates(demo_server: str) -> None:
    """The #40 regression check, on the page instead of the dialog (#129).

    Still worth having in a browser: the pencil pairs an `hx-confirm` with an
    `hx-on::after-request` side effect, and #40 was precisely a side effect that
    ran BEFORE htmx could issue its request. Only a real click through a real
    confirm proves the POST still happens.
    """
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(demo_server)

        # Library tab → first tile → the album's own page.
        page.click('[data-tab="library"]')
        page.locator('#panel-library a[href^="/album/"]').first.click()
        page.locator(_PENCIL).wait_for(state="visible")
        assert "/album/" in page.url

        # Accept the hx-confirm, then click the rematch pencil and require
        # that the POST is actually issued.
        page.on("dialog", lambda d: d.accept())
        with page.expect_request(lambda r: "/rematch" in r.url and r.method == "POST"):
            page.click(_PENCIL)

        # The album has left the Library, so the page navigates back to it
        # rather than sitting there describing something that moved.
        page.wait_for_url(f"{demo_server}/")
        browser.close()
