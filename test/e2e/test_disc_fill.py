"""Choosing a folder for a missing disc must reach the server (#876)."""

import os

import pytest
from test.e2e.conftest import open_review

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")

# The demo's split album: its second disc, adopted as an album of its own.
SECOND = "Sextina Aquafina/Aquafina After Dark (The Remixes)/Disc 2"


def test_a_folder_chosen_for_a_missing_disc_redraws_the_review(browser_factory, public_demo_server):
    base, _root = public_demo_server
    browser = browser_factory("chromium")
    page = browser.new_page(viewport={"width": 1280, "height": 1000})
    page.goto(base)
    card = page.locator('div[id^="task-"].relative').filter(has_text="Aquafina After Dark")
    disc = open_review(page, card).locator('[data-disc-fill="2"]')
    # The demo's second folder is the only one that fits, so it starts filled in.
    pw.expect(disc).to_contain_text(f"Disc 2 from {SECOND}")
    with page.expect_response(lambda r: "/assignments/" in r.url and r.request.method == "POST"):
        disc.get_by_role("button", name="Remove").click()
    picker = disc.get_by_role("combobox", name="Fill disc 2 from another folder")
    pw.expect(picker).to_be_visible()
    # A select commits on `change`, which reaches htmx only through the form's
    # trigger: the pregap checkbox's `change` listener used to swallow it.
    with page.expect_response(
        lambda r: "/assignments/" in r.url and r.request.method == "POST"
    ) as response:
        picker.select_option(label=SECOND)
    assert "fill_add=2%3A" in (response.value.request.post_data or "")
    pw.expect(disc).to_contain_text(f"Disc 2 from {SECOND}")
    page.context.close()
