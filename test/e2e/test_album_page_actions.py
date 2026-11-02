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
from test.e2e.conftest import open_review

# Opt-in like every other module here: `make check` stays browser-free.
pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1", reason="e2e disabled (set RUN_E2E=1)"
)
playwright_sync = pytest.importorskip("playwright.sync_api")

# Seeded as NEEDS_MBID with a store URL, and demo's MusicBrainz resolves that URL
# to exactly one release — automatic URL search should list it for selection.
# Reached by its title rather than by id, which also exercises the card link.
ALBUM_TITLE = "We Are Here To Make You Sad"


@pytest.mark.parametrize("on_album", [False, True])
def test_dismissed_suggestion_returns_to_selectable_store_results(
    browser_factory, reset_demo_server, htmx_ready, on_album
):
    browser = browser_factory("chromium")
    page = browser.new_page()
    page.goto(reset_demo_server)
    card = page.locator('div[id^="task-"].relative').filter(has_text="Gimme Some Money")
    playwright_sync.expect(card).to_have_count(1)
    aid = card.get_attribute("id").removeprefix("task-")
    if on_album:
        page.goto(f"{reset_demo_server}/album/{aid}")
    host = page.locator("#album-inbox-actions") if on_album else card
    tools = host.locator(f"#mbid-tools-{aid}")
    playwright_sync.expect(tools).to_be_hidden()
    review = open_review(page, host)
    dismiss = review.get_by_role("button", name="Dismiss suggestion", exact=True)
    playwright_sync.expect(dismiss).to_be_visible()
    page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")

    with page.expect_response(lambda r: "/reject/" in r.url):
        dismiss.click()
    playwright_sync.expect(review).to_have_count(0)
    playwright_sync.expect(tools).to_be_visible()
    playwright_sync.expect(tools.get_by_role("radio", name="By name", exact=True)).to_be_visible()
    # Pasting an MBID is the cluster's last mode (#880), behind its radio.
    playwright_sync.expect(tools.get_by_role("radio", name="By MBID", exact=True)).to_be_visible()
    # Polling must neither hide the tools again nor re-suggest the rejected release.
    if not on_album:
        with page.expect_response(lambda r: r.url.endswith("/tasks")):
            page.evaluate("htmx.trigger(document.body, 'tasks-changed')")
        page.wait_for_load_state("networkidle")
        playwright_sync.expect(tools).to_be_visible()
    # A fresh search lists the release; only Use opens another review.
    tools.get_by_role("radio", name="By name", exact=True).check()
    with page.expect_response(lambda r: "/candidates" in r.url):
        tools.get_by_role("radio", name="Store URL", exact=True).check()
    rows = tools.get_by_role("list", name="Release candidates").get_by_role("listitem")
    playwright_sync.expect(rows).to_have_count(1)
    playwright_sync.expect(rows).to_contain_text("Store URL matches")
    playwright_sync.expect(tools).to_be_visible()
    htmx_ready(rows.get_by_role("button", name="Use", exact=True)).press("Enter")
    # The release just chosen opens its review without another click (#933).
    playwright_sync.expect(dismiss).to_be_visible()
    playwright_sync.expect(tools).to_be_hidden()
    dialogs = []

    def dismiss_dialog(dialog):
        dialogs.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", dismiss_dialog)
    # The click starts tag writing; the card can leave only after the
    # confirmation finishes, which can exceed an assertion's timeout in CI.
    with page.expect_response(lambda r: r.url.endswith(f"/confirm/{aid}/accept")) as applied:
        htmx_ready(review.get_by_role("button", name="Confirm suggestion", exact=True)).click()
    assert "confirmation-applied" in applied.value.headers.get("hx-trigger", "")
    assert dialogs == []
    if on_album:
        page.wait_for_url("**/album/demo-rel-thamesmen*")
    else:
        playwright_sync.expect(card).to_have_count(0)
    page.context.close()


def test_automatic_url_search_runs_once_across_inbox_refreshes(
    browser_factory, reset_demo_server: str
) -> None:
    browser = browser_factory("chromium")
    page = browser.new_page()
    lookups: list[str] = []
    page.on("request", lambda r: lookups.append(r.url) if "/candidates" in r.url else None)
    page.goto(reset_demo_server)
    card = page.locator('div[id^="task-"].relative').filter(has_text=ALBUM_TITLE)
    results = card.get_by_role("list", name="Release candidates")
    playwright_sync.expect(results.get_by_role("button", name="Use", exact=True)).to_be_visible()
    page.wait_for_load_state("networkidle", timeout=10_000)
    assert len(lookups) == 1
    # A real inbox swap must preserve the form without repeating its load search.
    with page.expect_response(lambda r: r.url.endswith("/tasks")):
        page.evaluate("htmx.trigger(document.body, 'tasks-changed')")
    # The response arrives before HTMX finishes reinitializing the swapped
    # controls. Network-idle may already have fired for the initial load.
    page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")
    assert len(lookups) == 1
    # An explicit retry asks MusicBrainz again and keeps results selectable.
    with page.expect_response(lambda r: "/candidates" in r.url):
        card.get_by_role("button", name="Search", exact=True).click()
    page.wait_for_load_state("networkidle", timeout=10_000)
    assert len(lookups) == 2
    playwright_sync.expect(results.get_by_role("button", name="Use", exact=True)).to_be_visible()
    page.context.close()


def test_a_card_links_to_its_album_page_and_the_decision_can_be_taken_there(
    browser_factory,
    reset_demo_server: str,
    htmx_ready,
) -> None:
    """Both halves of #150 in the one path a user actually walks: from the card,
    to the album, to the decision."""
    browser = browser_factory("chromium")
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
    results = page.get_by_role("list", name="Release candidates")
    playwright_sync.expect(results).to_contain_text("Store URL matches")
    htmx_ready(results.get_by_role("button", name="Use", exact=True)).click()
    playwright_sync.expect(page.get_by_role("heading", name="Suggested match:")).to_be_visible()
    assert any("/candidates" in u for u in requests)
    assert page.locator("#album-tags").count() == 0
    page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")
    with page.expect_response(lambda r: "/reject/" in r.url, timeout=15_000) as got:
        page.get_by_role("button", name="Dismiss suggestion", exact=True).click()
    assert got.value.ok
    playwright_sync.expect(page.get_by_role("radio", name="By name", exact=True)).to_be_checked()
    # Selecting URL is an explicit fresh search after dismissal.
    with page.expect_response(lambda r: "/candidates" in r.url):
        page.get_by_role("radio", name="Store URL", exact=True).check()
    htmx_ready(results.get_by_role("button", name="Use", exact=True)).click()
    playwright_sync.expect(page.get_by_role("heading", name="Suggested match:")).to_be_visible()

    page.context.close()
