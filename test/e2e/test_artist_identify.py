"""Filling in an artist NFO from its page, in a browser (#981).

The rung pytest can't reach: whether Search, Look up and Identify land anywhere
at all. The page's own refresh container carries `hx-select`, and a request from
inside it that inherits that picks nothing out of its answer and shows
nothing."""

import os
import re

import pytest

from harmonist import demo

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")


def test_an_empty_nfo_is_found_identified_and_undone(browser_factory, reset_public_demo_server):
    base = reset_public_demo_server
    wyld = demo.MB_RELEASES["demo-rel-wyld"]["artist-credit"][0]["artist"]["id"]
    page = browser_factory("chromium").new_page(viewport={"width": 1280, "height": 900})
    page.goto(f"{base}/artists?artist_q=Wyld")
    rows = page.get_by_role("list", name="Artists", exact=True)
    rows.get_by_role("link").filter(has_text="Not identified").click()
    pw.expect(page.get_by_role("heading", name="Wyld Stallion", exact=True)).to_be_visible()
    empty_page = page.url.split("?")[0]

    finder = page.locator(".find-mode")
    finder.get_by_role("button", name="Search").click()
    candidates = finder.get_by_role("list", name="Artist candidates")
    candidate = candidates.get_by_role("listitem").filter(has_text="San Dimas rock duo")
    pw.expect(candidate.get_by_role("link", name=re.compile("Wyld Stallion"))).to_have_attribute(
        "href", f"https://musicbrainz.org/artist/{wyld}"
    )

    candidate.get_by_role("button", name="Identify").click()
    pw.expect(page).to_have_url(re.compile(rf"/artist/{wyld}"))
    history = page.locator(".album-history")
    pw.expect(history).to_contain_text("Identified")

    page.once("dialog", lambda prompt: prompt.accept())
    history.get_by_role("button", name="Undo this write's NFO changes").click()
    pw.expect(page).to_have_url(re.compile(rf"^{re.escape(empty_page)}"))
    pw.expect(page.locator(".album-warning__badge")).to_have_text("Not identified")
    page.context.close()


def test_a_pasted_mbid_shows_who_it_is_and_writes_nothing(
    browser_factory, reset_public_demo_server
):
    base = reset_public_demo_server
    wyld = demo.MB_RELEASES["demo-rel-wyld"]["artist-credit"][0]["artist"]["id"]
    page = browser_factory("chromium").new_page(viewport={"width": 1280, "height": 900})
    page.goto(f"{base}/artists?artist_q=Wyld")
    page.get_by_role("list", name="Artists", exact=True).get_by_role("link").filter(
        has_text="Not identified"
    ).click()
    finder = page.locator(".find-mode")
    finder.get_by_label("By MBID").check()
    finder.get_by_label("MusicBrainz artist URL or MBID").fill(
        f"https://musicbrainz.org/artist/{wyld}"
    )
    finder.get_by_role("button", name="Look up").click()
    candidate = finder.get_by_role("list", name="Artist candidates").get_by_role("listitem")
    pw.expect(candidate).to_contain_text("San Dimas rock duo")
    pw.expect(candidate.get_by_role("button", name="Identify")).to_be_visible()
    pw.expect(page.locator(".album-warning__badge")).to_have_text("Not identified")

    # The page redraws itself when a scan publishes: the choice of how to find,
    # and what was found, have to survive it.
    page.locator("#artist-detail").evaluate(
        "detail => { detail.dataset.beforeRedraw = '1'; htmx.trigger(document.body, 'tasks-changed') }"
    )
    pw.expect(page.locator("#artist-detail[data-before-redraw]")).to_have_count(0)
    pw.expect(finder.get_by_label("By MBID")).to_be_checked()
    pw.expect(finder.get_by_role("button", name="Look up")).to_be_visible()
    pw.expect(candidate).to_contain_text("San Dimas rock duo")
    page.context.close()
