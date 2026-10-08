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


def test_an_image_is_measured_when_it_is_shown(reset_carousel_server: str) -> None:
    """Each image's size line is measured the first time the carousel shows
    it, and no sooner: a hidden slide asks for nothing (#659)."""
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1400})
        measured: list[str] = []
        page.on(
            "request",
            lambda r: measured.append(r.url.split("/")[-2]) if r.url.endswith("/facts") else None,
        )
        page.goto(f"{reset_carousel_server}/album/{ALBUM_ID}")
        page.wait_for_selector("#album-artwork .art-pick__picker")
        page.wait_for_function(SETTLED)
        assert measured == []  # below the fold: nobody has seen it yet
        slide = page.locator("#album-artwork .art-pick__picker .art-pick__slide:visible")
        slide.scroll_into_view_if_needed()
        pw.expect(slide.locator(".art-row__meta")).to_contain_text("×")
        assert measured == ["101"]

        page.locator("#album-artwork").get_by_role("button", name="Next image").click()
        pw.expect(slide.locator(".art-row__meta")).to_contain_text("×")
        assert "103" in measured and "102" not in measured
        browser.close()


def test_an_offered_image_opens_full_size(reset_carousel_server: str) -> None:
    """Pressing the carousel's image opens its full-size view, as a row's
    does, fetching that one original and no other (#659)."""
    with pw.sync_playwright() as playwright:
        browser, page = _open(playwright, reset_carousel_server)
        originals: list[str] = []
        page.on(
            "request",
            lambda r: (
                originals.append(r.url.split("/")[-2]) if r.url.endswith("/original") else None
            ),
        )
        image = page.locator("#album-artwork .art-pick__picker .art-pick__slide:visible button")
        image.scroll_into_view_if_needed()
        assert originals == []  # nothing fetched to show nothing

        image.click()

        viewer = page.locator(f"#art-cand-full-{ALBUM_ID}-101")
        full = viewer.locator("img")
        pw.expect(viewer).to_be_visible()
        # The viewer is given its source by the popover's toggle event, which
        # arrives after the popover shows — and until then the image has no
        # src, which already counts as `complete`.
        pw.expect(full).to_have_attribute("src", f"/artwork/candidate/{ALBUM_ID}/101/original")
        pw.expect(full).to_have_js_property("complete", True)
        assert full.evaluate("img => img.naturalWidth") > 0
        pw.expect(viewer.get_by_role("button", name="Close artwork viewer")).to_be_focused()
        assert originals == ["101"]
        browser.close()


def test_a_section_swap_does_not_scroll_the_page_in_webkit(reset_carousel_server: str) -> None:
    """WebKit scrolls the window, synchronously, when the section's form is
    replaced — tens of pixels, with nothing on the page changing size (#659).
    Chromium never shows it, so only this engine can tell whether the section
    is put back where it was."""
    with pw.sync_playwright() as playwright:
        try:
            browser = playwright.webkit.launch()
        except pw.Error:
            pytest.skip("WebKit is not installed (playwright install webkit)")
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"{reset_carousel_server}/album/{ALBUM_ID}")
        page.wait_for_selector("#album-artwork .art-pick__picker")
        page.wait_for_function(SETTLED)
        page.locator("#album-artwork .art-pick__picker").scroll_into_view_if_needed()
        page.evaluate("window.scrollBy(0, 200)")
        page.wait_for_timeout(300)
        before = page.evaluate("scrollY")

        page.evaluate(
            """() => htmx.ajax('GET', location.pathname + '/artwork',
                              {target: '#album-artwork', swap: 'innerHTML settle:0ms'})"""
        )
        page.wait_for_timeout(500)
        page.wait_for_function(SETTLED)

        assert page.evaluate("scrollY") == before
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


def test_the_carousel_offers_no_step_past_either_end(reset_carousel_server: str) -> None:
    """‹ and › only where they go somewhere, as the rows' ↑ ↓ stop at the
    ends: no ‹ on the first image, and no › on the last once no release can
    follow it — where it was offered and did nothing."""
    with pw.sync_playwright() as playwright:
        browser, page = _open(playwright, reset_carousel_server)
        section = page.locator("#album-artwork")
        # By class, not role: a role locator skips a hidden button, and would
        # find "hidden" true of a button it simply did not find.
        back = section.locator(".art-pick__picker .art-pick__turn--back")
        on = section.locator(".art-pick__picker .art-pick__turn--on")

        pw.expect(back).to_be_hidden()  # 101, the first
        pw.expect(on).to_be_visible()
        with page.expect_response(lambda r: "more=ahead" in r.url):
            on.click()
        page.wait_for_function(SETTLED)
        # 103, the album's last image — but another release may follow it.
        pw.expect(back).to_be_visible()
        pw.expect(on).to_be_visible()

        on.click()  # 201, the other release's only image, and the group's last
        assert _shown(page) == ["201"]
        pw.expect(on).to_be_hidden()
        pw.expect(back).to_be_visible()
        browser.close()


def test_a_late_archive_check_leaves_the_picker_where_it_was_moved(
    reset_carousel_server: str,
) -> None:
    """Moving the picker makes no request, so a check sent before the move
    answers for where the picker WAS — and swapped in as served, it put the
    picker back there: the picker jumped, apparently at random (#659)."""
    with pw.sync_playwright() as playwright:
        browser, page = _open(playwright, reset_carousel_server)
        section = page.locator("#album-artwork")
        held = []
        page.route("**/artwork?reread=1*", lambda route: held.append((route, route.fetch())))
        page.locator('[hx-target="#album-artwork"][hx-get$="?reread=1"]').click()
        page.wait_for_function("() => document.querySelector('.htmx-request')")

        section.locator(".art-row").nth(1).locator(".art-row__select").click()
        section.get_by_role("checkbox", name="Front only").uncheck()
        section.get_by_role("button", name="Next image").click()  # 101 → 102, the back
        while not held:
            page.wait_for_timeout(50)
        route, response = held.pop()
        with page.expect_response(lambda r: "reread=1" in r.url):
            route.fulfill(response=response)
        page.wait_for_function(SETTLED)

        checked = section.locator("input[name='row']:checked")
        pw.expect(checked).to_have_value(
            section.locator("input[name='row']").nth(1).get_attribute("value") or ""
        )
        assert _shown(page) == ["102"]
        pw.expect(section.get_by_role("checkbox", name="Front only")).not_to_be_checked()
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
