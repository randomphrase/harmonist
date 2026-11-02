"""The Review dialog opens over the page, from a release row's Review (#933).

Closing it is the browser's half of the contract: Esc and the close button are
native, and closing must write nothing, whether the review was of a suggestion
Harmonist stored or of a release the user chose. The routes' half is in
`test/test_review_dialog.py`.
"""

import os

import pytest
from test.e2e.conftest import _run_demo_server, open_review, review_dialog

from harmonist import sidecar

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")

SUGGESTED = "Gimme Some Money"


def _suggestion(page, base):
    page.goto(f"{base}/?tab=inbox")
    card = page.locator('div[id^="task-"].relative').filter(has_text=SUGGESTED)
    pw.expect(card).to_have_count(1)
    return card, card.get_attribute("id").removeprefix("task-")


def _writes(page):
    """Requests that decide about a release. Searches are POSTs too (another
    card's automatic one included), but they only list."""
    sent = []
    page.on(
        "request",
        lambda r: sent.append(r.url) if "/reject/" in r.url or "/confirm/" in r.url else None,
    )
    return sent


@pytest.mark.parametrize("on_album", [False, True])
def test_a_found_suggestion_waits_until_it_is_decided(
    browser_factory, reset_demo_server, htmx_ready, on_album
):
    base = reset_demo_server
    page = browser_factory("chromium").new_page(viewport={"width": 1280, "height": 900})
    card, aid = _suggestion(page, base)
    if on_album:
        page.goto(f"{base}/album/{aid}")
    host = page.locator("#album-inbox-actions") if on_album else card
    suggestion = host.get_by_role("listitem", name="Suggested release")
    writes = _writes(page)

    dialog = open_review(page, host)
    confirm = dialog.get_by_role("button", name="Confirm suggestion", exact=True)
    pw.expect(confirm).to_be_enabled()
    # Not now: the suggestion keeps waiting, and its row offers it again.
    page.keyboard.press("Escape")
    pw.expect(dialog).to_have_count(0)
    pw.expect(suggestion).to_be_visible()

    open_review(page, host)
    pw.expect(confirm).to_be_enabled()
    with page.expect_response(lambda r: r.url.endswith(f"/reject/{aid}") and r.ok):
        htmx_ready(dialog.get_by_role("button", name="Dismiss suggestion", exact=True)).click()
    pw.expect(dialog).to_have_count(0)
    pw.expect(suggestion).to_have_count(0)
    # Only Dismiss wrote anything; Esc sent nothing.
    assert writes == [f"{base}/reject/{aid}"]
    page.context.close()


def test_confirming_closes_the_review_over_the_inbox(
    browser_factory, reset_demo_server, htmx_ready
):
    base = reset_demo_server
    page = browser_factory("chromium").new_page(viewport={"width": 1280, "height": 900})
    card, aid = _suggestion(page, base)
    dialog = open_review(page, card)
    confirm = dialog.get_by_role("button", name="Confirm suggestion", exact=True)
    with page.expect_response(lambda r: f"/confirm/{aid}/accept" in r.url and r.ok):
        htmx_ready(confirm).click()
    pw.expect(dialog).to_have_count(0)
    pw.expect(card).to_have_count(0)
    page.context.close()


def test_the_inbox_refreshing_leaves_an_open_review_alone(
    browser_factory, reset_demo_server, htmx_ready
):
    base = reset_demo_server
    page = browser_factory("chromium").new_page(viewport={"width": 1280, "height": 900})
    card, _ = _suggestion(page, base)
    dialog = open_review(page, card)
    htmx_ready(dialog.get_by_role("button", name="Edit track assignments", exact=True)).click()
    accept = dialog.get_by_role("button", name="Accept changes", exact=True)
    pw.expect(accept).to_be_visible()
    with page.expect_response(lambda r: "/tasks" in r.url and r.ok):
        page.evaluate("htmx.trigger(document.body, 'tasks-changed')")
    pw.expect(card.get_by_role("listitem", name="Suggested release")).to_be_visible()
    # The draft is where it was: still editing, in the same open dialog.
    pw.expect(dialog).to_be_visible()
    pw.expect(accept).to_be_visible()
    page.context.close()


def test_a_long_review_keeps_its_actions_in_view(browser_factory, reset_demo_server, htmx_ready):
    base = reset_demo_server
    height = 480
    page = browser_factory("chromium").new_page(viewport={"width": 1280, "height": height})
    card, _ = _suggestion(page, base)
    dialog = open_review(page, card)
    confirm = dialog.get_by_role("button", name="Confirm suggestion", exact=True)
    pw.expect(confirm).to_be_visible()
    pw.expect(dialog.locator(".assignment-artwork")).to_contain_text("CAA checked")
    # Taller than the viewport, so the dialog scrolls, and Confirm is in view.
    assert dialog.evaluate("d => d.scrollHeight > d.clientHeight")
    box = confirm.bounding_box()
    assert box is not None and box["y"] + box["height"] <= height
    page.context.close()


@pytest.fixture
def paste_server(tmp_path):
    for base in _run_demo_server(tmp_path, app_module="test.e2e.barcode_app:app"):
        yield base, tmp_path / "harmonist-demo" / "LFO" / "Frequencies"


@pytest.mark.parametrize("on_album", [False, True])
def test_a_chosen_release_is_reviewed_without_being_stored(
    browser_factory, paste_server, htmx_ready, on_album
):
    base, folder = paste_server
    aid = "barcode-demo"
    mbid = "b1a3a871-16c2-4e8b-b775-22785decf471"
    page = browser_factory("chromium").new_page(viewport={"width": 1280, "height": 900})
    with page.expect_response(lambda r: r.url.endswith(f"/manual/{aid}/barcode")):
        page.goto(f"{base}/album/{aid}" if on_album else f"{base}/?tab=inbox")
    panel = page.locator("#album-inbox-actions" if on_album else f"#task-{aid}")
    before = sidecar.read(folder)
    writes = _writes(page)

    dialog = open_review(page, panel, mbid)
    pw.expect(dialog.get_by_role("button", name="Confirm suggestion", exact=True)).to_be_visible()
    assert sidecar.read(folder) == before

    page.keyboard.press("Escape")
    pw.expect(dialog).to_have_count(0)
    # Closing decided nothing and wrote nothing; the choice is there to review again.
    assert writes == []
    assert sidecar.read(folder) == before
    field = panel.get_by_placeholder("MB release URL or 36-char MBID")
    pw.expect(field).to_have_value(mbid)
    htmx_ready(
        panel.locator('form[hx-get^="/review/"]').get_by_role("button", name="Review")
    ).click()
    pw.expect(review_dialog(page)).to_be_visible()
    page.context.close()
