"""Confirm closes the review at once; the album is written behind it (#1053).

The review used to stay open, modal, until the write had finished, which for a
large album on a NAS was over a minute: long enough for a reverse proxy to give
up, leaving the review stranded while the server finished. Here the write is
held at its first file, which a review waiting for its answer could never get
past, and the page is checked while it is held and after it is let go.
"""

import os

import pytest
from test.e2e.conftest import open_review

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")

SUGGESTED = "Gimme Some Money"
HTMX = {"HX-Request": "true"}


def test_confirm_closes_the_review_while_the_album_is_written(
    browser_factory, write_progress_server, htmx_ready
):
    base = write_progress_server
    page = browser_factory("chromium").new_page(viewport={"width": 1280, "height": 900})
    page.goto(f"{base}/?tab=inbox")
    card = page.locator('div[id^="task-"].relative').filter(has_text=SUGGESTED)
    pw.expect(card).to_have_count(1)
    aid = card.get_attribute("id").removeprefix("task-")
    dialog = open_review(page, card)
    confirm = dialog.get_by_role("button", name="Confirm suggestion", exact=True)
    assert page.request.post(f"{base}/test/write/arm/tags", headers=HTMX).ok

    with page.expect_response(lambda r: f"/confirm/{aid}/accept" in r.url and r.ok):
        htmx_ready(confirm).click()
    # Answered and closed with the write held at its first file.
    pw.expect(dialog).to_have_count(0)
    pw.expect(card.locator(f"#tagging-{aid}")).to_be_visible()
    bar = page.locator("#harmonist-status")
    pw.expect(bar).to_contain_text("Matching")

    # Let the write go: disarmed, so the held file is the last one to wait.
    assert page.request.post(f"{base}/test/write/arm/none", headers=HTMX).ok
    assert page.request.post(f"{base}/test/write/continue", headers=HTMX).ok
    pw.expect(card).to_have_count(0)
    pw.expect(bar).to_contain_text("Matched")
    pw.expect(bar).to_contain_text(SUGGESTED)
    page.context.close()


def test_a_write_that_fails_uncovers_its_card_and_says_so(
    browser_factory, write_progress_server, htmx_ready
):
    # It changes nothing a scan compares, so it publishes no new scan: only the
    # write's own end can tell the Inbox to draw the card without its cover.
    base = write_progress_server
    page = browser_factory("chromium").new_page(viewport={"width": 1280, "height": 900})
    page.goto(f"{base}/?tab=inbox")
    card = page.locator('div[id^="task-"].relative').filter(has_text=SUGGESTED)
    pw.expect(card).to_have_count(1)
    aid = card.get_attribute("id").removeprefix("task-")
    dialog = open_review(page, card)
    confirm = dialog.get_by_role("button", name="Confirm suggestion", exact=True)
    assert page.request.post(f"{base}/test/write/arm/fail", headers=HTMX).ok

    with page.expect_response(lambda r: f"/confirm/{aid}/accept" in r.url and r.ok):
        htmx_ready(confirm).click()
    pw.expect(dialog).to_have_count(0)
    bar = page.locator("#harmonist-status")
    pw.expect(bar).to_contain_text("Tagging failed")
    pw.expect(bar).to_contain_text("disk full")
    # Still waiting, as nothing was written, and open to another try.
    pw.expect(card).to_have_count(1)
    pw.expect(card.locator(f"#tagging-{aid}")).to_be_hidden()
    suggestion = card.get_by_role("listitem", name="Suggested release")
    pw.expect(suggestion.get_by_role("button", name="Review", exact=True)).to_be_enabled()
    page.context.close()
