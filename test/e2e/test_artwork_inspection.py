"""Image geometry, scale and selection need a browser, not rendered HTML."""

import os
import re
from urllib.parse import parse_qs, urlsplit

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")


def _open(page, base, review):
    page.goto(f"{base}/album/demo-rel-dingoes")
    if review:
        page.locator("#contribution-editions-demo-rel-dingoes").get_by_role(
            "listitem", name="Suggested digital release"
        ).get_by_role("button", name="Use", exact=True).click()
        host = page.get_by_role("region", name="Review suggested release").locator(
            ".assignment-artwork"
        )
        # These tests inspect the complete gallery. Current images are already
        # usable before the asynchronous selected-release image arrives.
        pw.expect(host.get_by_alt_text("Artwork for selected release", exact=True)).to_be_visible()
    else:
        host = page.locator("#album-artwork")
    pw.expect(host.locator("button.art-row__art").first).to_be_visible()
    return host


def _arrows(viewer):
    """‹ and ›, by position: a role locator skips the one hidden at an end."""
    return (
        viewer.locator(".art-full__nav > .art-arrow:first-child"),
        viewer.locator(".art-full__nav > .art-arrow:last-child"),
    )


def _switch(viewer):
    """Step to the gallery's other image — on, or back from the last — and
    return its viewer's id."""
    back, on = _arrows(viewer)
    arrow = back if on.is_disabled() else on
    target = arrow.get_attribute("data-art-view")
    arrow.click()
    return target


def _assert_fitted(viewer):
    image = viewer.locator("img")
    image.evaluate("e => e.decode()")
    # Drawn by the popover's toggle event, which arrives after it shows.
    pw.expect(image).to_have_attribute("style", re.compile(r"width"))
    geometry = image.evaluate("""e => {
        const stage = e.closest('.art-full__stage');
        return {
            image: e.getBoundingClientRect().toJSON(),
            stage: stage.getBoundingClientRect().toJSON(),
            scale: Math.min(1, stage.clientWidth / e.naturalWidth,
                              stage.clientHeight / e.naturalHeight),
            width: e.naturalWidth, height: e.naturalHeight,
        };
    }""")
    rect, stage = geometry["image"], geometry["stage"]
    assert rect["width"] == pytest.approx(geometry["width"] * geometry["scale"], abs=1)
    assert rect["height"] == pytest.approx(geometry["height"] * geometry["scale"], abs=1)
    assert rect["left"] >= stage["left"] - 1
    assert rect["top"] >= stage["top"] - 1
    assert rect["right"] <= stage["right"] + 1
    assert rect["bottom"] <= stage["bottom"] + 1


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("dismiss", [False, True], ids=["open", "dismissed"])
def test_background_refresh_preserves_artwork_inspection(
    browser_factory, artwork_refresh_server, engine, dismiss
):
    browser = browser_factory(engine)
    page = browser.new_page(viewport={"width": 900, "height": 700})
    held = []

    def hold(route):
        held.append((route, route.fetch()))
        page.evaluate("window.archiveResponseHeld = true")

    page.route("**/artwork?check=1", hold)
    host = _open(page, artwork_refresh_server, False)
    page.wait_for_function("window.archiveResponseHeld === true")
    # Finish the initial comparison's autofocus processing while the CAA
    # response stays held; only the later artwork swap is under test.
    page.wait_for_function("!document.querySelector('.htmx-settling')")
    thumb = host.locator("button.art-row__art[popovertarget]").first
    thumb.click()
    viewer = page.locator(".art-full:popover-open")
    # Switch away from the opener's image: Escape must still return to the
    # original thumbnail after the refresh replaces both thumbnails.
    selected = _switch(viewer)
    pw.expect(viewer).to_have_attribute("id", selected)
    viewer.locator("[data-art-zoom]").click()
    stage = viewer.get_by_role("region", name="Artwork detail")
    stage.evaluate("e => { e.scrollLeft = 200; e.scrollTop = 150; }")
    stage.focus()
    if dismiss:
        page.keyboard.press("Escape")
        pw.expect(viewer).to_have_count(0)
    page.evaluate("""() => {
            window.artworkBeforeRefresh = document.querySelector('#album-artwork .art-row');
            document.body.addEventListener('htmx:afterSettle', e => {
                if (e.target.id === 'album-artwork') window.artworkRefreshed = true;
            });
        }""")
    route, response = held.pop()
    route.fulfill(response=response)
    page.wait_for_function("window.artworkRefreshed === true")
    assert page.evaluate("!window.artworkBeforeRefresh.isConnected")
    if dismiss:
        pw.expect(viewer).to_have_count(0)
    else:
        pw.expect(viewer).to_have_attribute("id", selected)
        pw.expect(viewer.locator("[data-art-zoom]")).to_have_attribute("aria-pressed", "true")
        pw.expect(stage).to_be_focused()
        assert stage.evaluate("e => [e.scrollLeft, e.scrollTop]") == [200, 150]
        page.keyboard.press("Escape")
        pw.expect(viewer).to_have_count(0)
        pw.expect(thumb).to_be_focused()
    page.context.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("review", [False, True], ids=["album", "review"])
@pytest.mark.parametrize("check", ["fit", "focus"])
def test_open_and_switch_fit_the_selected_image_without_focusing_a_menu(
    browser_factory, artwork_inspection_server, engine, review, check
):
    browser = browser_factory(engine)
    page = browser.new_page(viewport={"width": 1280, "height": 882}, has_touch=True)
    host = _open(page, artwork_inspection_server, review)
    thumb = host.locator("button.art-row__art[popovertarget]").first
    target = thumb.get_attribute("popovertarget")
    thumb.click()
    viewer = page.locator(".art-full:popover-open")
    pw.expect(viewer).to_have_attribute("id", target)
    if check == "fit":
        _assert_fitted(viewer)
    else:
        pw.expect(viewer.get_by_role("button", name="Close artwork viewer")).to_be_focused()
    # On to the last image and back to the first: the 'focus' run by the
    # keyboard's ← and →, which leave it on the arrow they stand for while
    # it goes somewhere, and on Close where it stops.
    seen = [target]
    for which, key in enumerate(["ArrowRight", "ArrowLeft"]):
        while not _arrows(viewer)[1 - which].is_disabled():
            arrow = _arrows(viewer)[1 - which]
            image_id = arrow.get_attribute("data-art-view")
            if check == "fit":
                arrow.click()
            else:
                page.keyboard.press(key)
            pw.expect(viewer).to_have_attribute("id", image_id)
            seen.append(image_id)
            if check == "fit":
                _assert_fitted(viewer)
            elif _arrows(viewer)[1 - which].is_disabled():
                pw.expect(viewer.get_by_role("button", name="Close artwork viewer")).to_be_focused()
            else:
                pw.expect(_arrows(viewer)[1 - which]).to_be_focused()
    assert len(set(seen)) >= 2, seen
    pw.expect(viewer.locator(".art-full__count")).to_have_text(f"1 of {len(set(seen))}")
    page.keyboard.press("Escape")
    pw.expect(thumb).to_be_focused()
    page.context.close()


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("review", [False, True], ids=["album", "review"])
def test_thumbnail_frames_do_not_shrink_for_long_metadata(
    browser_factory, artwork_inspection_server, width, review
):
    browser = browser_factory("chromium")
    page = browser.new_page(viewport={"width": width, "height": 800})
    host = _open(page, artwork_inspection_server, review)
    # The rows' frames: the picker's image is sized by its own panel (#659).
    thumbs = host.locator("button.art-row__art[popovertarget]:not(.art-pick__image)")
    assert thumbs.count() >= 2
    # Stress one caption, leaving the opposing image's caption short.
    # No layout/style mutation: this is the metadata a large box set can carry.
    thumbs.first.evaluate(
        "e => e.nextElementSibling.textContent = "
        "'Discs 1, 3, 5, 7, 9: tracks 1, 3, 5, 7, 9 and cover.png — '"
        ".repeat(6)"
    )
    sizes = thumbs.evaluate_all(
        "els => els.map(e => [e.getBoundingClientRect().width, e.getBoundingClientRect().height])"
    )
    assert all(size == [104, 104] for size in sizes), sizes
    for thumb in thumbs.all():
        pw.expect(thumb.locator("img")).to_have_css("object-fit", "contain")
    assert host.evaluate("e => e.scrollWidth <= e.clientWidth + 1")
    page.context.close()


@pytest.mark.parametrize("review", [False, True], ids=["album", "review"])
@pytest.mark.parametrize("width", [900, 390])
def test_native_pixels_and_switching_preserve_scale_and_position(
    browser_factory, artwork_inspection_server, review, width
):
    browser = browser_factory("chromium")
    page = browser.new_page(viewport={"width": width, "height": 700})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    host = _open(page, artwork_inspection_server, review)
    thumb = host.locator("button.art-row__art[popovertarget]").first
    target = thumb.get_attribute("popovertarget")
    thumb.click()
    viewer = page.locator(f'[id="{target}"]')
    pw.expect(viewer).to_be_visible()
    zoom = viewer.locator("[data-art-zoom]")
    pw.expect(zoom).to_have_text(re.compile(r"^Fit · \d+%$"))
    zoom.click()
    image = viewer.locator("img")
    pw.expect(image).to_have_css("width", "1600px")
    pw.expect(image).to_have_css("height", "1000px")
    pw.expect(viewer).to_contain_text("1600×1000")
    pw.expect(zoom).to_have_text("100%")
    stage = viewer.get_by_role("region", name="Artwork detail")
    stage.evaluate("e => { e.scrollLeft = 200; e.scrollTop = 150; }")
    assert stage.evaluate("e => [e.scrollLeft, e.scrollTop]") == [200, 150]
    # On to the gallery's last image: in the review, the selected release's.
    shown = page.locator(".art-full:popover-open")
    next_id = target
    while not _arrows(shown)[1].is_disabled():
        next_id = _switch(shown)
        pw.expect(shown).to_have_attribute("id", next_id)
    assert next_id != target
    switched = page.locator(f'[id="{next_id}"]')
    pw.expect(switched).to_be_visible()
    pw.expect(switched.locator("[data-art-zoom]")).to_have_attribute("aria-pressed", "true")
    source = host.locator(f'button[popovertarget="{next_id}"] img').first.get_attribute("src")
    pw.expect(switched.locator("img")).to_have_attribute("src", source)
    if review:
        assert parse_qs(urlsplit(source).query)["replacement"] == [
            "demo-rel-dingoes:demo-rel-dingoes-digital"
        ]
        pw.expect(switched.locator("img")).to_have_css("width", "2000px")
    assert switched.locator("img").evaluate(
        "e => e.width === e.naturalWidth && e.height === e.naturalHeight"
    )
    assert switched.get_by_role("region", name="Artwork detail").evaluate(
        "e => [e.scrollLeft, e.scrollTop]"
    ) == [200, 150]
    switched.locator("[data-art-zoom]").click()
    _assert_fitted(switched)
    # Clicking the picture does the same, at 100% centring the spot clicked.
    picture = switched.locator("img")
    box = picture.bounding_box()
    picture.click(position={"x": box["width"] * 0.6, "y": box["height"] * 0.5})
    pw.expect(switched.locator("[data-art-zoom]")).to_have_attribute("aria-pressed", "true")
    centred = switched.get_by_role("region", name="Artwork detail").evaluate(
        """e => {
            const image = e.querySelector('img').getBoundingClientRect();
            return [e.getBoundingClientRect().left + e.clientWidth / 2 - image.left, image.width];
        }"""
    )
    assert centred[0] == pytest.approx(centred[1] * 0.6, abs=2)
    picture.click()
    _assert_fitted(switched)
    while shown.get_attribute("id") != target:
        _arrows(shown)[0].click()
    pw.expect(viewer).to_be_visible()
    _assert_fitted(viewer)
    page.keyboard.press("Escape")
    pw.expect(viewer).to_be_hidden()
    pw.expect(thumb).to_be_focused()
    assert not errors
    page.context.close()


@pytest.mark.parametrize("width", [650, 1280])
def test_review_images_align_below_wrapping_headings(
    browser_factory, artwork_inspection_server, width
):
    browser = browser_factory("chromium")
    page = browser.new_page(viewport={"width": width, "height": 800})
    host = _open(page, artwork_inspection_server, True)
    current = host.get_by_alt_text("Current artwork: All 3 tracks", exact=True)
    candidate = host.get_by_alt_text("Artwork for selected release", exact=True)
    pw.expect(candidate).to_be_visible()
    checkbox = host.get_by_role("checkbox", name="Use artwork from the selected release")
    for included in [False, True]:
        checkbox.set_checked(included)
        assert candidate.bounding_box()["y"] == pytest.approx(current.bounding_box()["y"], abs=1)
    pw.expect(host.get_by_text("Keep existing artwork", exact=True)).to_have_count(0)
    page.context.close()


def test_review_is_the_only_artwork_section_until_cancelled(
    browser_factory, artwork_inspection_server
):
    browser = browser_factory("chromium")
    page = browser.new_page()
    review_artwork = _open(page, artwork_inspection_server, True)
    pw.expect(review_artwork).to_be_visible()
    pw.expect(page.locator("#album-artwork")).to_be_hidden()
    page.get_by_role("region", name="Review suggested release").get_by_role(
        "button", name="Cancel", exact=True
    ).click()
    pw.expect(page.locator("#album-artwork")).to_be_visible()
    page.context.close()
