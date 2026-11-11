"""Identifying an artist NFO from its Inbox card, in a browser (#996).

The rung pytest can't reach: whether a card's Identify, inside the Inbox's own
refresh container, sends its request at all, and whether the browser stays in
the Inbox while the card leaves, rather than following the file to its artist
page as Identify there does."""

import os
from urllib.parse import urlsplit

import pytest

from harmonist import demo

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")


def test_a_folder_suggests_its_artist_and_identify_keeps_the_inbox(
    browser_factory, reset_public_demo_server
):
    base = reset_public_demo_server
    wyld = demo.MB_RELEASES["demo-rel-wyld"]["artist-credit"][0]["artist"]["id"]
    page = browser_factory("chromium").new_page(viewport={"width": 1280, "height": 900})
    page.goto(f"{base}/")
    section = page.locator("#inbox-artists")
    card = section.locator("[id^=artist-task-]").filter(has_text="Wyld Stallion/artist.nfo")
    pw.expect(card.get_by_role("heading", name="Wyld Stallion")).to_be_visible()
    contained = card.get_by_label("From contained albums")
    name_search = card.get_by_role("textbox", name="Artist name to search MusicBrainz for")
    suggestion = card.get_by_role("listitem", name="Suggested artist")
    # Its album waits to be linked, so it isn't in the Library yet and the
    # folder offers no one: the card searches by name.
    pw.expect(contained).to_be_disabled()
    pw.expect(name_search).to_be_visible()

    # Once the sync links it, the redraw replaces the finder rather than
    # keeping the old one, and the folder's artist is offered first.
    page.get_by_role("button", name="Sync Bandcamp").click()
    pw.expect(contained).to_be_checked(timeout=20_000)
    pw.expect(suggestion).to_contain_text("On 1 of 1 album")
    pw.expect(suggestion.get_by_role("link")).to_have_attribute(
        "href", f"https://musicbrainz.org/artist/{wyld}"
    )
    pw.expect(name_search).to_be_hidden()

    # The radios swap the panels, and the list is there to come back to.
    card.get_by_label("By name").check()
    pw.expect(name_search).to_be_visible()
    pw.expect(suggestion).to_be_hidden()
    contained.check()
    pw.expect(suggestion).to_be_visible()

    with page.expect_response(
        lambda r: r.request.method == "POST" and urlsplit(r.url).path.endswith("/identify")
    ) as answer:
        suggestion.get_by_role("button", name="Identify").click()
    assert answer.value.ok
    # The section, which only the Inbox has, counts one fewer: the card left
    # and the browser didn't go to the artist page.
    pw.expect(section.get_by_role("heading", name="Artists not identified")).to_have_text(
        "Artists not identified · 1"
    )
    pw.expect(card).to_have_count(0)
    assert urlsplit(page.url).path == "/"
    page.context.close()
