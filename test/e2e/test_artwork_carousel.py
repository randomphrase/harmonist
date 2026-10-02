"""The picker's carousel steps through the archive's images, and Front only
hides the rest (#659).

Both are a script moving a radio's check and the stylesheet showing the
checked image — no request — so the Python suite sees correct markup whether
or not either works. The server lists a front, a back and a second front for
every release (`artwork_carousel_app`).
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")

ALBUM_ID = "demo-rel-dingoes"


def _shown(page) -> list[str]:
    """The image ids the carousel is showing — one, if it works."""
    return page.locator(
        "#album-artwork .art-pick__picker .art-pick__slide:visible input[name='candidate']"
    ).evaluate_all("els => els.map(el => el.value)")


def test_the_carousel_steps_through_the_images_front_only_allows(carousel_server: str) -> None:
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1400})
        page.goto(f"{carousel_server}/album/{ALBUM_ID}")
        page.wait_for_selector("#album-artwork .art-pick__picker")
        page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")
        section = page.locator("#album-artwork")
        back = section.get_by_role("button", name="Previous image")
        on = section.get_by_role("button", name="Next image")
        front_only = section.get_by_role("checkbox", name="Front only")
        sent: list[str] = []
        page.on("request", lambda r: sent.append(r.url))

        # Ticked, since the archive has fronts: the back cover is stepped over.
        pw.expect(front_only).to_be_checked()
        assert _shown(page) == ["101"]
        on.click()
        assert _shown(page) == ["103"]
        on.click()  # the last front: stays rather than wrapping
        assert _shown(page) == ["103"]
        back.click()
        assert _shown(page) == ["101"]

        # Every image, once the box is unticked…
        front_only.uncheck()
        on.click()
        assert _shown(page) == ["102"]
        # …and ticking it while the back is shown moves to a front.
        front_only.check()
        assert _shown(page) == ["101"]
        assert not [u for u in sent if "/artwork?" in u or u.endswith("/artwork")], sent

        # Use sends the image the carousel shows.
        on.click()
        with page.expect_response(lambda r: "pick=1" in r.url) as used:
            section.locator("button.art-pick__use:visible").click()
        assert "candidate=103" in used.value.url
        browser.close()
