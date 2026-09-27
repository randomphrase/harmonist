"""Image geometry, scale and selection need a browser, not rendered HTML."""

import os
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
    else:
        host = page.locator("#album-artwork")
    pw.expect(host.locator("button.art-row__art").first).to_be_visible()
    return host


def _assert_fitted(viewer):
    image = viewer.locator("img")
    image.evaluate("e => e.decode()")
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
@pytest.mark.parametrize("review", [False, True], ids=["album", "review"])
@pytest.mark.parametrize("check", ["fit", "focus"])
def test_open_and_switch_fit_the_selected_image_without_focusing_a_menu(
    artwork_inspection_server, engine, review, check
):
    with pw.sync_playwright() as playwright:
        browser = getattr(playwright, engine).launch()
        page = browser.new_page(viewport={"width": 1280, "height": 882}, has_touch=True)
        host = _open(page, artwork_inspection_server, review)
        thumb = host.locator("button.art-row__art[popovertarget]").first
        target = thumb.get_attribute("popovertarget")
        thumb.click()
        viewer = page.locator(".art-full:popover-open")
        options = viewer.locator(".art-full__image-choice option").evaluate_all(
            "es => es.map(e => e.value)"
        )
        for index, image_id in enumerate([target, *options[1:], target]):
            if index:
                viewer.get_by_role("combobox", name="Artwork image").select_option(image_id)
            pw.expect(viewer).to_have_attribute("id", image_id)
            if check == "fit":
                _assert_fitted(viewer)
            else:
                pw.expect(viewer.get_by_role("button", name="Close artwork viewer")).to_be_focused()
        page.keyboard.press("Escape")
        pw.expect(thumb).to_be_focused()
        browser.close()


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("review", [False, True], ids=["album", "review"])
def test_thumbnail_frames_do_not_shrink_for_long_metadata(artwork_inspection_server, width, review):
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 800})
        host = _open(page, artwork_inspection_server, review)
        thumbs = host.locator("button.art-row__art[popovertarget]")
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
        browser.close()


@pytest.mark.parametrize("review", [False, True], ids=["album", "review"])
@pytest.mark.parametrize("width", [900, 390])
def test_native_pixels_and_switching_preserve_scale_and_position(
    artwork_inspection_server, review, width
):
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 700})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        host = _open(page, artwork_inspection_server, review)
        thumb = host.locator("button.art-row__art[popovertarget]").first
        target = thumb.get_attribute("popovertarget")
        thumb.click()
        viewer = page.locator(f'[id="{target}"]')
        pw.expect(viewer).to_be_visible()
        mode = viewer.get_by_role("combobox", name="Viewing scale")
        mode.select_option("native")
        image = viewer.locator("img")
        pw.expect(image).to_have_css("width", "1600px")
        pw.expect(image).to_have_css("height", "1000px")
        pw.expect(viewer).to_contain_text("1600×1000")
        pw.expect(viewer.locator("output")).to_have_text("100% · 1 image pixel per CSS pixel")
        stage = viewer.get_by_role("region", name="Artwork detail")
        stage.evaluate("e => { e.scrollLeft = 200; e.scrollTop = 150; }")
        assert stage.evaluate("e => [e.scrollLeft, e.scrollTop]") == [200, 150]
        select = viewer.get_by_role("combobox", name="Artwork image")
        options = select.locator("option").evaluate_all("els => els.map(e => e.value)")
        assert len(options) == len(set(options)) >= 2
        next_id = options[-1]
        assert next_id != target
        select.select_option(next_id)
        switched = page.locator(f'[id="{next_id}"]')
        pw.expect(switched).to_be_visible()
        pw.expect(switched.get_by_role("combobox", name="Viewing scale")).to_have_value("native")
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
        switched.get_by_role("combobox", name="Viewing scale").select_option("fit")
        _assert_fitted(switched)
        switched.get_by_role("combobox", name="Artwork image").select_option(target)
        pw.expect(viewer).to_be_visible()
        _assert_fitted(viewer)
        page.keyboard.press("Escape")
        pw.expect(viewer).to_be_hidden()
        pw.expect(thumb).to_be_focused()
        assert not errors
        browser.close()


@pytest.mark.parametrize("width", [650, 1280])
def test_review_images_align_below_wrapping_headings(artwork_inspection_server, width):
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 800})
        host = _open(page, artwork_inspection_server, True)
        current = host.get_by_alt_text("Current artwork: All 3 tracks", exact=True)
        candidate = host.get_by_alt_text("Artwork for selected release", exact=True)
        pw.expect(candidate).to_be_visible()
        checkbox = host.get_by_role("checkbox", name="Use artwork from the selected release")
        for included in [False, True]:
            checkbox.set_checked(included)
            assert candidate.bounding_box()["y"] == pytest.approx(
                current.bounding_box()["y"], abs=1
            )
        pw.expect(host.get_by_text("Keep existing artwork", exact=True)).to_have_count(0)
        browser.close()


def test_review_is_the_only_artwork_section_until_cancelled(artwork_inspection_server):
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        review_artwork = _open(page, artwork_inspection_server, True)
        pw.expect(review_artwork).to_be_visible()
        pw.expect(page.locator("#album-artwork")).to_be_hidden()
        page.get_by_role("region", name="Review suggested release").get_by_role(
            "button", name="Cancel", exact=True
        ).click()
        pw.expect(page.locator("#album-artwork")).to_be_visible()
        browser.close()
