"""Image geometry, scale and selection need a browser, not rendered HTML."""

import os
from urllib.parse import parse_qs, urlsplit

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")


def _open(page, base, review):
    page.goto(f"{base}/album/demo-rel-dingoes")
    if review:
        page.locator("#contribution-editions-demo-rel-dingoes tbody tr").filter(
            has_text="Suggested"
        ).get_by_role("button", name="Use", exact=True).click()
        host = page.get_by_role("region", name="Review suggested release").locator(
            ".assignment-artwork"
        )
    else:
        host = page.locator("#album-artwork")
    pw.expect(host.locator("button.art-row__art").first).to_be_visible()
    return host


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
        fit_scale = switched.locator("img").evaluate("e => e.width / e.naturalWidth")
        assert 0 < fit_scale < 1
        assert switched.locator("img").evaluate("e => e.height / e.naturalHeight") == pytest.approx(
            fit_scale, abs=0.001
        )
        switched.get_by_role("combobox", name="Artwork image").select_option(target)
        pw.expect(viewer).to_be_visible()
        assert viewer.locator("img").evaluate("e => e.width / e.naturalWidth") == pytest.approx(
            fit_scale, abs=0.001
        )
        page.keyboard.press("Escape")
        pw.expect(viewer).to_be_hidden()
        pw.expect(thumb).to_be_focused()
        assert not errors
        browser.close()
