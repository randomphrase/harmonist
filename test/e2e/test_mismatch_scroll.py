"""A mismatch review opens over the album page and leaves it where it was (#933).

It used to replace the picker inline, which had to scroll its start into view
without pulling a heading behind the sticky navigation. In a dialog the page
underneath doesn't move at all, from a mouse or the keyboard, and Cancel comes
back to it unchanged.
"""

import os

import pytest
from test.e2e.conftest import review_dialog

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize("digital_contribution_server", ["linked"], indirect=True)
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("width", [1400, 390])
@pytest.mark.parametrize("via", ["click", "keyboard"])
def test_mismatch_review_opens_over_the_page(
    browser_factory, digital_contribution_server, htmx_ready, engine, width, via
):
    server, _ = digital_contribution_server
    browser = browser_factory(engine)
    page = browser.new_page(viewport={"width": width, "height": 720})
    page.goto(f"{server}/album/demo-rel-dingoes")
    panel = page.locator("#album-contributions-demo-rel-dingoes")
    use = htmx_ready(panel.get_by_role("button", name="Review", exact=True).first)
    pw.expect(use).to_be_visible()
    page.wait_for_function("!document.querySelector('.htmx-settling')")
    use.scroll_into_view_if_needed()
    before = page.evaluate("window.scrollY")
    with page.expect_response(lambda r: "/assignments/" in r.url and "cancel=true" in r.url):
        if via == "click":
            use.click()
        else:
            use.focus()
            page.keyboard.press("Enter")
    review = review_dialog(page)
    pw.expect(review.locator(".assignment-content")).to_be_visible()
    # Include the delayed artwork swap and browser scroll anchoring.
    pw.expect(review.get_by_alt_text("Artwork for selected release", exact=True)).to_be_visible()
    page.wait_for_function("!document.querySelector('.htmx-settling, .htmx-request')")
    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )
    # The review starts in view, and the page behind it hasn't moved.
    assert review.bounding_box()["y"] >= 0
    assert page.evaluate("window.scrollY") == pytest.approx(before, abs=1)
    # Keyboard focus stays in the review, never the picker behind it.
    page.keyboard.press("Tab")
    assert review.evaluate("e => e.contains(document.activeElement)")
    review.get_by_role("button", name="Cancel", exact=True).click()
    pw.expect(review).to_have_count(0)
    pw.expect(use).to_be_visible()
    assert page.evaluate("window.scrollY") == pytest.approx(before, abs=1)
    page.context.close()
