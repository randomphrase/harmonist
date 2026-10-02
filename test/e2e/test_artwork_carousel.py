"""The picker's carousel steps through the archive's images, Front only hides
the rest, and past the last image it goes on to another release (#659).

Stepping is a script moving a radio's check and the stylesheet showing the
checked image — no request — and going on to another release is the script
pressing a hidden button at the right moment. The Python suite sees correct
markup whether or not any of it works. The server lists a front, a back and a
second front for every release, and gives every group another release with
one image (`artwork_carousel_app`).
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")

ALBUM_ID = "demo-rel-dingoes"
SETTLED = "() => !document.querySelector('.htmx-request, .htmx-settling')"


def _shown(page) -> list[str]:
    """The image ids the carousel is showing — one, if it works."""
    return page.locator(
        "#album-artwork .art-pick__picker .art-pick__slide:visible input[name='candidate']"
    ).evaluate_all("els => els.map(el => el.value)")


def _open(playwright, base: str):
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 1400})
    page.goto(f"{base}/album/{ALBUM_ID}")
    page.wait_for_selector("#album-artwork .art-pick__picker")
    page.wait_for_function(SETTLED)
    return browser, page


def test_front_only_steps_over_the_back_cover(reset_carousel_server: str) -> None:
    with pw.sync_playwright() as playwright:
        browser, page = _open(playwright, reset_carousel_server)
        section = page.locator("#album-artwork")
        on = section.get_by_role("button", name="Next image")
        back = section.get_by_role("button", name="Previous image")
        front_only = section.get_by_role("checkbox", name="Front only")
        sent: list[str] = []
        page.on("request", lambda r: sent.append(r.url))

        # Ticked, since the archive has fronts.
        pw.expect(front_only).to_be_checked()
        assert _shown(page) == ["101"]
        # Every image, once the box is unticked…
        front_only.uncheck()
        on.click()
        assert _shown(page) == ["102"]
        back.click()
        on.click()
        assert _shown(page) == ["102"]
        # …and ticking it while the back is shown moves to a front.
        front_only.check()
        assert _shown(page) == ["101"]
        # The section was never asked for again: only thumbnails were fetched.
        assert not [u for u in sent if f"/album/{ALBUM_ID}/artwork" in u], sent
        browser.close()


def test_the_carousel_goes_on_to_another_release(reset_carousel_server: str) -> None:
    """Arriving at the last image lists the next release, so › past it is
    instant — and Use sends whichever image is shown."""
    with pw.sync_playwright() as playwright:
        browser, page = _open(playwright, reset_carousel_server)
        section = page.locator("#album-artwork")
        on = section.get_by_role("button", name="Next image")

        with page.expect_response(lambda r: "more=ahead" in r.url):
            on.click()  # 101 → 103, the last front: look ahead
        page.wait_for_function(SETTLED)
        assert _shown(page) == ["103"]

        sent: list[str] = []
        page.on("request", lambda r: sent.append(r.url))
        on.click()
        assert _shown(page) == ["201"]
        assert not [u for u in sent if "more=" in u], sent
        slide = section.locator(".art-pick__picker .art-pick__slide:visible")
        pw.expect(slide).to_contain_text("Another release")
        pw.expect(slide).to_contain_text("Japanese edition")

        with page.expect_response(lambda r: "pick=1" in r.url) as used:
            section.locator("button.art-pick__use:visible").click()
        assert "candidate=201" in used.value.url
        browser.close()


def test_next_past_the_last_image_steps_when_nothing_looked_ahead(
    reset_carousel_server: str,
) -> None:
    """Without a look ahead in hand — it failed, or was still on its way —
    › on the last image asks for the next release itself, and shows it."""
    with pw.sync_playwright() as playwright:
        browser, page = _open(playwright, reset_carousel_server)
        section = page.locator("#album-artwork")
        on = section.get_by_role("button", name="Next image")
        page.route("**/artwork?*more=ahead*", lambda route: route.abort())

        on.click()  # 101 → 103; the look ahead is lost
        page.wait_for_function(SETTLED)
        assert _shown(page) == ["103"]

        with page.expect_response(lambda r: "more=step" in r.url):
            on.click()
        page.wait_for_function(SETTLED)
        assert _shown(page) == ["201"]
        browser.close()
