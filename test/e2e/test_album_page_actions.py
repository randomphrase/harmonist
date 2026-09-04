"""The inbox's decisions, taken on the album page (#150).

This is the #40 bug class twice over, and the Python suite can see neither half.
It asserts that the right `hx-post` and the right `hx-on::after-request` came
back in the markup — which was equally true of the buttons in #40 that silently
did nothing. Whether a click produces a *request*, and whether the page then
comes back re-rendered rather than sitting there looking unchanged, is only
observable in a browser.

The stakes: on the album page these controls are the only way out of Needs MBID
for a reader who followed a card's link. A dead one strands them exactly where
#150 was filed to stop stranding them, and it looks completely fine.
"""

from __future__ import annotations

import os

import pytest

# Opt-in like every other module here: `make check` stays browser-free.
pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1", reason="e2e disabled (set RUN_E2E=1)"
)
playwright_sync = pytest.importorskip("playwright.sync_api")

# Seeded as NEEDS_MBID with a store URL, and demo's MusicBrainz resolves that URL
# to exactly one release — so Recheck tags it and the album lands in the Library.
# Reached by its title rather than by id, which also exercises the card link.
ALBUM_TITLE = "We Are Here To Make You Sad"


def test_a_card_links_to_its_album_page_and_the_decision_can_be_taken_there(
    demo_server: str,
) -> None:
    """Both halves of #150 in the one path a user actually walks: from the card,
    to the album, to the decision."""
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()

        requests: list[str] = []
        page.on("request", lambda r: requests.append(r.url))

        page.goto(demo_server)
        # The inbox is pulled into the page after load, so wait for the card.
        link = page.get_by_role("link", name=ALBUM_TITLE)
        link.wait_for(timeout=20_000)
        link.click()

        page.wait_for_url("**/album/**", timeout=10_000)
        # The page is one you can ACT on, not only read: the actions section is
        # the thing #150 added, and Recheck is this album's way out of the inbox.
        page.wait_for_selector("#album-inbox-actions", timeout=10_000)
        recheck = page.get_by_role("button", name="Recheck")
        recheck.wait_for(timeout=10_000)

        assert not [u for u in requests if "/recheck/" in u], "nothing pressed yet"

        with page.expect_response(lambda r: "/recheck/" in r.url, timeout=15_000) as got:
            recheck.click()
        assert got.value.ok

        # ...and the page came BACK. `#album-tags` is rendered server-side and
        # only for an album that has a MusicBrainz release, so its appearance is
        # two facts at once: the recheck tagged the album, and the reload that
        # `reload_unless_retargeted` fires actually happened. Without the reload
        # the page would sit on the untagged render with no section at all.
        page.wait_for_selector("#album-tags", timeout=15_000)
        # ...and the album has left the inbox: the actions section that offered
        # Recheck is gone, because there is no longer a release to find. Its
        # presence a moment ago is what makes this absence worth asserting.
        assert page.locator("#album-inbox-actions").count() == 0

        browser.close()
