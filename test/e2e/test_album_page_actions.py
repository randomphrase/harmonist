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
# to exactly one release — automatic URL search should suggest it for review.
# Reached by its title rather than by id, which also exercises the card link.
ALBUM_TITLE = "We Are Here To Make You Sad"


@pytest.mark.parametrize("on_album", [False, True])
def test_search_tools_wait_until_suggestion_is_dismissed(reset_demo_server, on_album):
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(reset_demo_server)
        card = page.locator('div[id^="task-"].relative').filter(has_text="Gimme Some Money")
        playwright_sync.expect(card).to_have_count(1)
        aid = card.get_attribute("id").removeprefix("task-")
        if on_album:
            page.goto(f"{reset_demo_server}/album/{aid}")
        host = page.locator("#album-inbox-actions") if on_album else card
        tools = host.locator(f"#mbid-tools-{aid}")
        dismiss = host.get_by_role("button", name="Dismiss suggestion", exact=True)
        playwright_sync.expect(dismiss).to_be_visible()
        page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")
        playwright_sync.expect(tools).to_be_hidden()

        with page.expect_response(lambda r: "/reject/" in r.url):
            dismiss.click()
        playwright_sync.expect(tools).to_be_visible()
        playwright_sync.expect(tools.get_by_role("radio", name="Name", exact=True)).to_be_visible()
        playwright_sync.expect(tools.locator('input[name="mbid"]')).to_be_visible()
        # Polling must neither hide the tools again nor re-suggest the rejected release.
        if not on_album:
            with page.expect_response(lambda r: r.url.endswith("/tasks")):
                page.evaluate("htmx.trigger(document.body, 'tasks-changed')")
            page.wait_for_load_state("networkidle")
            playwright_sync.expect(tools).to_be_visible()
        # A deliberate new search offers a suggestion and hides the alternatives again.
        tools.get_by_role("radio", name="Name", exact=True).check()
        with page.expect_response(lambda r: "/candidates" in r.url):
            tools.get_by_role("radio", name="Store URL", exact=True).check()
        playwright_sync.expect(dismiss).to_be_visible()
        playwright_sync.expect(tools).to_be_hidden()
        browser.close()


def test_automatic_url_search_runs_once_across_inbox_refreshes(reset_demo_server: str) -> None:
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        lookups: list[str] = []
        page.on("request", lambda r: lookups.append(r.url) if "/candidates" in r.url else None)
        page.goto(reset_demo_server)
        card = page.locator('div[id^="task-"].relative').filter(has_text=ALBUM_TITLE)
        playwright_sync.expect(card.get_by_role("heading", name="Suggested match:")).to_be_visible()
        page.wait_for_load_state("networkidle", timeout=10_000)
        assert len(lookups) == 1
        # A real inbox swap must preserve the form without repeating its load search.
        with page.expect_response(lambda r: r.url.endswith("/tasks")):
            page.evaluate("htmx.trigger(document.body, 'tasks-changed')")
        # The response arrives before HTMX finishes reinitializing the swapped
        # controls. Network-idle may already have fired for the initial load.
        page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")
        assert len(lookups) == 1
        # Dismissal reveals the search tools; an explicit retry asks MusicBrainz again.
        with page.expect_response(lambda r: "/reject/" in r.url):
            card.get_by_role("button", name="Dismiss suggestion", exact=True).click()
        with page.expect_response(lambda r: "/candidates" in r.url):
            card.get_by_role("button", name="Search", exact=True).click()
        page.wait_for_load_state("networkidle", timeout=10_000)
        assert len(lookups) == 2
        browser.close()


def test_a_card_links_to_its_album_page_and_the_decision_can_be_taken_there(
    reset_demo_server: str,
) -> None:
    """Both halves of #150 in the one path a user actually walks: from the card,
    to the album, to the decision."""
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()

        requests: list[str] = []
        page.on("request", lambda r: requests.append(r.url))

        page.goto(reset_demo_server)
        # The inbox is pulled into the page after load, so wait for the card.
        link = page.locator('a[href^="/album/"]').filter(has_text=ALBUM_TITLE)
        link.wait_for(timeout=20_000)
        link.click()

        page.wait_for_url("**/album/**", timeout=10_000)
        page.wait_for_selector("#album-inbox-actions", timeout=10_000)
        playwright_sync.expect(page.get_by_role("heading", name="Suggested match:")).to_be_visible()
        assert any("/candidates" in u for u in requests)
        assert page.locator("#album-tags").count() == 0
        page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")
        with page.expect_response(lambda r: "/reject/" in r.url, timeout=15_000) as got:
            page.get_by_role("button", name="Dismiss suggestion", exact=True).click()
        assert got.value.ok
        playwright_sync.expect(page.get_by_role("radio", name="Name", exact=True)).to_be_checked()
        # Selecting URL is an explicit fresh search after dismissal.
        with page.expect_response(lambda r: "/candidates" in r.url):
            page.get_by_role("radio", name="Store URL", exact=True).check()
        playwright_sync.expect(page.get_by_role("heading", name="Suggested match:")).to_be_visible()

        browser.close()
