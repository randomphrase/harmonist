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

import itertools
import os
import re

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")

ALBUM_ID = "demo-rel-dingoes"
SETTLED = "() => !document.querySelector('.htmx-request, .htmx-settling')"
_MAXIMIZED = (
    f"() => !!document.getElementById('art-pick-panel-{ALBUM_ID}')?.matches(':popover-open')"
)


def _shown(page) -> list[str]:
    """The image ids the carousel is showing — one, if it works."""
    return page.locator(
        "#album-artwork .art-pick__picker .art-pick__slide:visible input[name='candidate']"
    ).evaluate_all("els => els.map(el => el.value)")


def _open(browser_factory, base: str, engine: str = "chromium"):
    browser = browser_factory(engine)
    page = browser.new_page(viewport={"width": 1280, "height": 1400})
    page.goto(f"{base}/album/{ALBUM_ID}")
    page.wait_for_selector("#album-artwork .art-pick__picker")
    page.wait_for_function(SETTLED)
    return browser, page


def test_front_only_steps_over_the_back_cover(browser_factory, reset_carousel_server: str) -> None:
    _browser, page = _open(browser_factory, reset_carousel_server)
    section = page.locator("#album-artwork")
    on = section.get_by_role("button", name="Next image")
    back = section.get_by_role("button", name="Previous image")
    front_only = section.get_by_role("checkbox", name="Show Front covers only")
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
    page.context.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("width", [390, 1440])
def test_rejected_image_does_not_stay_visible_when_stepping(
    browser_factory, reset_carousel_server: str, htmx_ready, engine: str, width: int
) -> None:
    """Rejecting a slide must not select it alongside the next image."""
    browser = browser_factory(engine)
    page = browser.new_page(viewport={"width": width, "height": 1000})
    page.goto(f"{reset_carousel_server}/album/{ALBUM_ID}")
    section = page.locator("#album-artwork")
    slide = section.locator(".art-pick__picker .art-pick__slide:visible")
    reject = slide.get_by_role("checkbox", name="Don't suggest this image")
    htmx_ready(reject).check()
    pw.expect(reject).to_be_checked()
    page.wait_for_function(SETTLED)
    assert _shown(page) == ["101"]

    section.get_by_role("button", name="Next image").click()
    page.wait_for_function(SETTLED)
    assert _shown(page) == ["103"]

    section.get_by_role("button", name="Previous image").click()
    assert _shown(page) == ["101"]
    pw.expect(reject).to_be_checked()
    htmx_ready(reject).uncheck()
    pw.expect(reject).not_to_be_checked()
    page.wait_for_function(SETTLED)
    assert _shown(page) == ["101"]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.context.close()


def test_an_image_is_measured_when_it_is_shown(browser_factory, reset_carousel_server: str) -> None:
    """Each image's size line is measured the first time the carousel shows
    it, and no sooner: a hidden slide asks for nothing (#659)."""
    browser = browser_factory("chromium")
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
    # The slide's image: on a wide page the slide lends its parts to the
    # carousel's layout and has no box of its own to scroll to (#918).
    slide.locator(".art-pick__image").scroll_into_view_if_needed()
    pw.expect(slide.locator(".art-row__meta")).to_contain_text("×")
    assert measured == ["101"]

    page.locator("#album-artwork").get_by_role("button", name="Next image").click()
    pw.expect(slide.locator(".art-row__meta")).to_contain_text("×")
    assert "103" in measured and "102" not in measured
    page.context.close()


def _maximize(page):
    """Press the carousel's image, which maximizes the picker on it (#918)."""
    image = page.locator("#album-artwork .art-pick__picker .art-pick__slide:visible button")
    image.scroll_into_view_if_needed()
    image.click()
    page.wait_for_function(_MAXIMIZED)
    return page.locator(f"#art-pick-panel-{ALBUM_ID}")


def _zoom_in(panel) -> None:
    """Click the archive's image for 100%: once its original is here, which is
    when the scale is offered — a click on the stand-in thumbnail does
    nothing, on purpose."""
    zoom = panel.locator("[data-art-zoom]")
    pw.expect(zoom).to_have_text(re.compile(r"^Fit · \d+%$"))  # drawn, so its state is real
    pw.expect(zoom).to_be_enabled()
    _pane(panel, "cand").click()
    pw.expect(zoom).to_have_attribute("aria-pressed", "true")


def _pane(panel, side: str):
    """What a maximized pane shows — an image, or a row's empty frame: one,
    if the generated rules work."""
    return panel.locator(f".art-max__stage--{side} .art-max__canvas > :visible")


def test_an_offered_image_opens_full_size(browser_factory, reset_carousel_server: str) -> None:
    """Pressing the carousel's image maximizes the picker on it, beside the
    checked row's image — fetching that one original and no other (#659,
    #918)."""
    _browser, page = _open(browser_factory, reset_carousel_server)
    originals: list[str] = []
    page.on(
        "request",
        lambda r: originals.append(r.url.split("/")[-2]) if r.url.endswith("/original") else None,
    )
    assert originals == []  # nothing fetched to show nothing
    # Narrower than either image, so both are scaled to fit — and drawing
    # them at one size is something the script has to do.
    page.set_viewport_size({"width": 700, "height": 600})

    panel = _maximize(page)

    full = _pane(panel, "cand")
    pw.expect(full).to_have_count(1)
    pw.expect(full).to_have_attribute("src", f"/artwork/candidate/{ALBUM_ID}/101/original")
    pw.expect(full).to_have_js_property("complete", True)
    assert full.evaluate("img => img.naturalWidth") > 0
    row = _pane(panel, "now")
    pw.expect(row).to_have_count(1)
    pw.expect(row).to_have_attribute("data-row", "1")
    # Drawn at one size, so a spot on one lines up with the same spot on the other.
    page.wait_for_function(
        """() => {
            const [a, b] = Array.from(document.querySelectorAll('.art-max__stage img'))
                .filter(i => i.checkVisibility()).map(i => i.getBoundingClientRect().width);
            return a > 0 && Math.abs(a - b) < 1;
        }"""
    )
    # The image pressed is hidden by the larger layout; the keyboard is on
    # the toggle instead, so the arrow keys reach the picker.
    pw.expect(panel.get_by_role("button", name="Put the artwork picker back")).to_be_focused()
    assert originals == ["101"]
    page.context.close()


def test_the_maximized_picker_steps_and_use_puts_it_back(
    browser_factory, reset_carousel_server: str
) -> None:
    """Maximized, ↑ ↓ step the rows and ← → the images, as the arrows do; a
    re-render of the section — the look ahead at the last image — leaves it
    maximized; and Use puts it back before its request goes (#918)."""
    _browser, page = _open(browser_factory, reset_carousel_server)
    panel = _maximize(page)
    section = page.locator("#album-artwork")

    page.keyboard.press("ArrowDown")
    pw.expect(section.locator("input[name='row']").nth(1)).to_be_checked()
    pw.expect(_pane(panel, "now")).to_have_attribute("data-row", "2")

    with page.expect_response(lambda r: "more=ahead" in r.url):
        page.keyboard.press("ArrowRight")  # 101 → 103, the last front: look ahead
    page.wait_for_function(SETTLED)
    assert page.evaluate(_MAXIMIZED)  # the look ahead's re-render kept it so
    assert _shown(page) == ["103"]
    pw.expect(_pane(panel, "cand")).to_have_attribute("data-cand", "103")
    # …of the album's two fronts and the other release's, now listed.
    pw.expect(panel.locator(".art-pick__slide[data-front='true']")).to_have_count(3)

    # Put back, the picker on the page is where it was moved to embiggened:
    # the same row, the same image — it is the same picker.
    panel.get_by_role("button", name="Put the artwork picker back").click()
    assert not page.evaluate(_MAXIMIZED)
    # Once the popover's own toggle event has run, which is when anything
    # done on putting it back would be done.
    pw.expect(panel).not_to_have_attribute("role", "dialog")
    pw.expect(section.locator("input[name='row']").nth(1)).to_be_checked()
    assert _shown(page) == ["103"]
    page.get_by_role("button", name="Embiggen").click()
    page.wait_for_function(_MAXIMIZED)
    pw.expect(_pane(panel, "cand")).to_have_attribute("data-cand", "103")

    use = panel.locator("button.art-pick__use--go:visible")
    with page.expect_response(lambda r: "pick=1" in r.url) as used:
        use.click()
        assert not page.evaluate(_MAXIMIZED)  # put back as it is pressed
    assert "candidate=103" in used.value.url
    page.wait_for_function(SETTLED)
    assert not page.evaluate(_MAXIMIZED)
    page.context.close()


def test_the_maximized_panes_zoom_and_pan_together(
    browser_factory, reset_carousel_server: str
) -> None:
    """Clicking an image takes both panes to 100%, and panning one pans the
    other (#918)."""
    _browser, page = _open(browser_factory, reset_carousel_server)
    page.set_viewport_size({"width": 700, "height": 500})
    panel = _maximize(page)
    pw.expect(panel.locator("[data-art-zoom]")).to_have_text(re.compile(r"^Fit · \d+%$"))

    _zoom_in(panel)

    assert panel.locator(".art-max__stage--now").evaluate("e => e.scrollWidth > e.clientWidth")
    # The wheel, as a user pans: the pane under it leads, the other follows.
    for lead, follower in (("cand", "now"), ("now", "cand")):
        pane = panel.locator(f".art-max__stage--{lead}")
        box = pane.bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        before = pane.evaluate("e => [e.scrollLeft, e.scrollTop]")
        page.mouse.wheel(30, 20)
        page.wait_for_function(
            """([lead, follower, before]) => {
                const a = document.querySelector(lead), b = document.querySelector(follower);
                return (a.scrollLeft !== before[0] || a.scrollTop !== before[1])
                    && a.scrollLeft === b.scrollLeft && a.scrollTop === b.scrollTop;
            }""",
            arg=[f".art-max__stage--{lead}", f".art-max__stage--{follower}", before],
        )
    page.context.close()


def test_a_pan_step_is_not_undone_by_the_other_pane_following(
    browser_factory, reset_carousel_server: str
) -> None:
    """Following the lead queues the follower's own scroll event, and a frame's
    scroll events run in the order they were queued — so the lead's next step,
    taken before that event runs, was copied back over by it: the lead jumped
    back to where it had been, and a pan stuttered (#918)."""
    _browser, page = _open(browser_factory, reset_carousel_server)
    page.set_viewport_size({"width": 700, "height": 500})
    panel = _maximize(page)
    _zoom_in(panel)
    box = panel.locator(".art-max__stage--cand").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.wheel(0, 1)  # the user takes hold of the right pane

    settled = page.evaluate(
        """async () => {
            const frame = () => new Promise(r => requestAnimationFrame(() => setTimeout(r)));
            const [follower, lead] = document.querySelectorAll('.art-max__stage');
            for (let i = 0; i < 3; i++) await frame();
            // Just after the follower has followed, and before its own scroll
            // event has run: the user's next step.
            lead.addEventListener('scroll', () => { lead.scrollLeft = 40; }, {once: true});
            lead.scrollLeft = 20;
            for (let i = 0; i < 4; i++) await frame();
            return [lead.scrollLeft, follower.scrollLeft];
        }"""
    )
    assert settled == [40, 40]
    page.context.close()


_LAID_OUT = """() => [
    ...document.querySelectorAll(
        '#album-artwork :is(.art-pick__picker, .art-max__stage, .art-pick__picker label)')
].filter(e => e.checkVisibility()).map(e => {
    const b = e.getBoundingClientRect();
    return [Math.round(b.top + scrollY), Math.round(b.bottom + scrollY)];
})"""


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("width", [700, 1280])
def test_a_longer_caption_moves_nothing(
    browser_factory, reset_carousel_server: str, engine: str, width: int
) -> None:
    """The back cover's caption has a line the fronts' lack, its kind; stepping
    onto it and off again moves nothing, embiggened or on the page — the room
    for the longest caption is kept whichever is shown (#918). And beside the
    last row, the picker stays inside the section that makes room for it.
    Embiggened, the panel is laid out alike at either width; on the page, the
    picker is a column of its own only at the wider one."""
    _browser, page = _open(browser_factory, reset_carousel_server, engine)
    page.set_viewport_size({"width": width, "height": 1400})
    section = page.locator("#album-artwork")
    section.get_by_role("checkbox", name="Show Front covers only").uncheck()
    panel = _maximize(page)
    caption = panel.locator(".art-pick__slide:visible .art-row__facts")
    before = page.evaluate(_LAID_OUT)
    # Each caption level with the other: the two views' rows are one grid's.
    row_caption = panel.locator(".art-max__head--now .art-row__facts:visible")
    assert row_caption.bounding_box()["y"] == caption.bounding_box()["y"]

    panel.get_by_role("button", name="Next image").click()
    assert _shown(page) == ["102"]
    pw.expect(caption).to_contain_text("Back")
    assert page.evaluate(_LAID_OUT) == before

    panel.get_by_role("button", name="Put the artwork picker back").click()
    pw.expect(panel).not_to_have_attribute("role", "dialog")
    before = page.evaluate(_LAID_OUT)
    section.get_by_role("button", name="Previous image").click()
    assert _shown(page) == ["101"]
    pw.expect(caption).not_to_contain_text("Back")
    assert page.evaluate(_LAID_OUT) == before

    section.locator("input[name='row']").last.evaluate("radio => radio.click()")
    bottoms = page.evaluate(
        """() => ['.art-pick__picker', '.art-layout'].map(s =>
            document.querySelector('#album-artwork ' + s).getBoundingClientRect().bottom)"""
    )
    assert bottoms[0] <= bottoms[1], bottoms
    page.context.close()


def test_the_room_kept_for_the_picker_ends_where_it_does(
    browser_factory, reset_carousel_server: str
) -> None:
    """Beside the rows, the picker hangs from the checked row, and the section
    ends at whichever reaches lower, the picker or the rows and Apply — not at
    the foot of a picker beside the last row, which left an empty row's height
    under one beside the first (#918)."""
    _browser, page = _open(browser_factory, reset_carousel_server)
    rows = page.locator("#album-artwork input[name='row']")
    assert rows.count() > 1
    for index in range(rows.count()):
        rows.nth(index).evaluate("radio => radio.click()")
        layout, foot = page.evaluate(
            """() => {
                const s = document.getElementById('album-artwork');
                const bottom = e => e ? e.getBoundingClientRect().bottom : 0;
                return [bottom(s.querySelector('.art-layout')), Math.max(
                    ...['.art-pick__picker', '.art-row:last-child', '.art-apply']
                        .map(sel => bottom(s.querySelector(sel))))];
            }"""
        )
        assert abs(layout - foot) <= 1, (index, layout, foot)
    page.context.close()


def test_the_three_images_are_drawn_alike(browser_factory, reset_carousel_server: str) -> None:
    """Now, After Apply and the picker's image are the same size, their tops
    level and each caption to its image's right, with Use level with their
    middles — whichever row is checked (#918). They are three views of one
    choice; a picker drawn differently read as something else."""
    _browser, page = _open(browser_factory, reset_carousel_server)
    rows = page.locator("#album-artwork input[name='row']")
    for index in range(rows.count()):
        rows.nth(index).evaluate("radio => radio.click()")
        boxes = page.evaluate(
            """() => {
                const s = document.getElementById('album-artwork');
                const box = e => { const b = e.getBoundingClientRect();
                    return [b.left, b.top, b.width, b.height]; };
                const shown = sel => [...s.querySelectorAll(sel)].filter(e => e.checkVisibility());
                const images = shown('.art-row:has(input[name="row"]:checked) :is(.art-row__art, '
                    + '.art-row__slot, .art-row__unchanged), .art-pick__slide .art-pick__image');
                // An empty After Apply has no caption to place.
                const captions = images.map(i => i.parentElement.querySelector(
                    ':scope > .art-row__facts')).map(c => c && box(c));
                const use = shown('.art-pick__for')[0];
                return {images: images.map(box), captions, use: box(use)};
            }"""
        )
        images, captions, use = boxes["images"], boxes["captions"], boxes["use"]
        assert len(images) == 3, (index, boxes)
        # Level, and one size — to within the half pixel the picker's 1.5px
        # border loses where a browser draws it whole, at a pixel ratio of 1.
        for i in range(1, 4):
            assert max(b[i] for b in images) - min(b[i] for b in images) <= 0.5, (index, images)
        assert captions[0] and captions[-1], (index, boxes)  # the row's, and the picker's
        for image, caption in zip(images, captions, strict=True):
            if caption:
                assert caption[0] >= image[0] + image[2], (index, image, caption)
        _, top, size, _ = images[0]
        assert abs(use[1] + use[3] / 2 - (top + size / 2)) <= 1, (index, use, images)
    page.context.close()


def test_the_picker_is_the_height_of_its_row(browser_factory, reset_carousel_server: str) -> None:
    """Beside the rows, the picker's outline runs from the checked row's top
    divider to its bottom one — not a few pixels over, which read as nearly
    the row's height and not quite (#918). ↑ and ↓ stand at its top and
    bottom, centred on Use's label rather than on the arrow it is drawn in."""
    _browser, page = _open(browser_factory, reset_carousel_server)
    rows = page.locator("#album-artwork input[name='row']")
    for index in range(rows.count()):
        rows.nth(index).evaluate("radio => radio.click()")
        boxes = page.evaluate(
            """() => {
                const s = document.getElementById('album-artwork');
                const box = e => { const b = e.getBoundingClientRect();
                    return {top: b.top, bottom: b.bottom, centre: (b.left + b.right) / 2}; };
                const shown = sel => [...s.querySelectorAll(sel)].find(e => e.checkVisibility());
                const use = shown('.art-pick__for .art-pick__use');
                const b = use.getBoundingClientRect(), c = getComputedStyle(use);
                const left = b.left + parseFloat(c.paddingLeft);
                const right = b.right - parseFloat(c.paddingRight);
                return {
                    row: box(s.querySelector('.art-row:has(input[name="row"]:checked)')),
                    picker: box(s.querySelector('.art-pick__picker')),
                    up: box(s.querySelector('.art-pick__step[data-art-step="-1"]')),
                    down: box(s.querySelector('.art-pick__step[data-art-step="1"]')),
                    label: (left + right) / 2,
                };
            }"""
        )
        row, picker, up, down = boxes["row"], boxes["picker"], boxes["up"], boxes["down"]
        assert abs(picker["top"] - row["top"]) <= 0.5, (index, boxes)
        assert abs(picker["bottom"] - row["bottom"]) <= 0.5, (index, boxes)
        assert abs(up["top"] - picker["top"]) <= 0.5, (index, boxes)
        assert abs(down["bottom"] - picker["bottom"]) <= 0.5, (index, boxes)
        for arrow in (up, down):
            assert abs(arrow["centre"] - boxes["label"]) <= 1, (index, boxes)
    page.context.close()


@pytest.mark.parametrize("embiggened", [False, True], ids=["page", "embiggened"])
def test_use_and_its_check_are_one_size(
    browser_factory, reset_carousel_server: str, embiggened: bool
) -> None:
    """Use, and the ✓ in its place where the row already has the shown image,
    are one size, so stepping between images moves neither it nor ↑ ↓ (#918).
    Each is measured in turn in the checked row's place, as the section's
    generated rules show one or the other."""
    _browser, page = _open(browser_factory, reset_carousel_server)
    if embiggened:
        _maximize(page)
    sizes = page.evaluate(
        """() => {
            const place = [...document.querySelectorAll('#album-artwork .art-pick__for')]
                .find(e => e.checkVisibility());
            const shown = [...place.children].find(e => e.checkVisibility());
            const size = el => {
                const was = [shown.style.display, el.style.display];
                shown.style.display = 'none';
                el.style.display = 'block';
                const b = el.getBoundingClientRect();
                [shown.style.display, el.style.display] = was;
                return [b.width, b.height];
            };
            return ['.art-pick__use--go', '.art-pick__use--done']
                .map(sel => size(place.querySelector(sel)));
        }"""
    )
    assert sizes[0] == sizes[1], sizes
    page.context.close()


_BOXES = """sels => {
    const box = e => { const b = e.getBoundingClientRect();
        return {left: b.left, right: b.right, top: b.top, bottom: b.bottom,
                width: b.width, x: (b.left + b.right) / 2, y: (b.top + b.bottom) / 2}; };
    // A key ending in "text" measures the element's words, not its box.
    const words = el => { const r = document.createRange(); r.selectNodeContents(el); return r; };
    return Object.fromEntries(Object.entries(sels).map(([key, sel]) => {
        const el = [...document.querySelectorAll(sel)].find(e => e.checkVisibility());
        return [key, el ? box(key.endsWith('text') ? words(el) : el) : null];
    }));
}"""


def _either_end_of(arrows: tuple[dict, dict], title: dict, column: dict) -> None:
    """The arrows at either end of the column, as far in from each, both level
    with the title line and the title's words between them."""
    first, second = arrows
    inset = first["left"] - column["left"]
    assert 0 <= inset <= 16 and abs(column["right"] - second["right"] - inset) <= 1, (
        arrows,
        column,
    )
    assert first["right"] <= title["left"] and title["right"] <= second["left"], (arrows, title)
    for arrow in arrows:
        assert abs(arrow["y"] - title["y"]) <= 1, (arrow, title)


@pytest.mark.parametrize("width", [700, 1024, 1440])
def test_the_picker_steps_from_its_title(
    browser_factory, reset_carousel_server: str, width: int
) -> None:
    """‹ before the picker's title and › at the end of its line, with no count
    (#918): ‹ never moves as the title changes, and › keeps to the end. The
    title is one line, cut short rather than wrapped. Beside the rows, the
    caption has at least the room a row's has, and under it Don't suggest,
    then Front only. At the first row the gutter's ↑ is there, disabled, so ↓
    reads as one of a pair."""
    _browser, page = _open(browser_factory, reset_carousel_server)
    page.set_viewport_size({"width": width, "height": 1400})
    section = page.locator("#album-artwork")
    section.locator("input[name='row']").first.evaluate("radio => radio.click()")
    p = "#album-artwork .art-pick__picker"
    sels = {
        "row": "#album-artwork .art-row:has(input[name='row']:checked) .art-row__facts",
        "caption": f"{p} .art-pick__slide .art-row__facts",
        "title_text": f"{p} .art-pick__slide .art-row__who",
        "meta": f"{p} .art-pick__slide .art-row__meta",
        "nav": f"{p} .art-pick__nav",
        "back": f"{p} .art-pick__turn--back",
        "on": f"{p} .art-pick__turn--on",
        "dismiss": f"{p} .art-pick__slide .art-pick__dismiss",
        "front": f"{p} .art-pick__front-only",
    }
    first = page.evaluate(_BOXES, sels)
    with page.expect_response(lambda r: "more=ahead" in r.url):
        section.get_by_role("button", name="Next image").click()  # 103: both ways open
    page.wait_for_function(SETTLED)
    boxes = page.evaluate(_BOXES, sels)
    _either_end_of((boxes["back"], boxes["on"]), boxes["title_text"], boxes["caption"])
    assert boxes["back"]["left"] == first["back"]["left"], (first, boxes)  # ‹ stays put
    # Under the title, the caption starts where ‹ does, not in line with the title.
    assert abs(boxes["meta"]["left"] - boxes["back"]["left"]) <= 1, boxes
    title = section.locator(".art-pick__picker .art-pick__slide .art-row__who").filter(visible=True)
    pw.expect(title).to_have_css("white-space", "nowrap")
    pw.expect(section.locator(".art-pick__picker .art-pick__nav")).to_have_text("")
    if width >= 1024:
        assert boxes["caption"]["width"] >= boxes["row"]["width"], boxes
        order = [boxes[k] for k in ("caption", "dismiss", "front")]
        for above, below in itertools.pairwise(order):
            assert above["bottom"] <= below["top"], boxes
        assert len({round(b["left"]) for b in order}) == 1, boxes  # one column

    up = section.locator(".art-pick__step[data-art-step='-1']")
    pw.expect(up).to_be_visible()
    pw.expect(up).to_be_disabled()
    pw.expect(section.locator(".art-pick__step[data-art-step='1']")).to_be_enabled()
    page.context.close()


def test_embiggened_the_arrows_frame_the_titles(
    browser_factory, reset_carousel_server: str
) -> None:
    """Embiggened, ‹ and › stand at either end of the shown image's title line
    and ↑ and ↓ at either end of the row's, with no counts (#918). Under the
    shown image: its caption, Don't suggest, Front only — the page's order —
    and the row's caption level with it."""
    _browser, page = _open(browser_factory, reset_carousel_server)
    panel = _maximize(page)
    p = ".art-pick__picker:popover-open"
    sels = {
        "row": f"{p} .art-max__head--now .art-row__facts",
        "row_title_text": f"{p} .art-max__head--now .art-row__who",
        "caption": f"{p} .art-pick__slide .art-row__facts",
        "title_text": f"{p} .art-pick__slide .art-row__who",
        "up": f"{p} .art-pick__step[data-art-step='-1']",
        "down": f"{p} .art-pick__step[data-art-step='1']",
        "back": f"{p} .art-pick__turn--back",
        "on": f"{p} .art-pick__turn--on",
        "dismiss": f"{p} .art-pick__slide .art-pick__dismiss",
        "front": f"{p} .art-pick__front-only",
    }
    page.keyboard.press("ArrowDown")
    with page.expect_response(lambda r: "more=ahead" in r.url):
        page.keyboard.press("ArrowRight")  # 103: both ways open
    page.wait_for_function(SETTLED)
    boxes = page.evaluate(_BOXES, sels)
    _either_end_of((boxes["up"], boxes["down"]), boxes["row_title_text"], boxes["row"])
    _either_end_of((boxes["back"], boxes["on"]), boxes["title_text"], boxes["caption"])
    pw.expect(panel.locator(".art-pick__nav")).to_have_text("")
    order = [boxes[k] for k in ("caption", "dismiss", "front")]
    for above, below in itertools.pairwise(order):
        assert above["bottom"] <= below["top"], boxes
    assert boxes["row"]["top"] == boxes["caption"]["top"], boxes
    page.context.close()


def test_a_section_swap_does_not_scroll_the_page_in_webkit(
    browser_factory, reset_carousel_server: str, htmx_complete
) -> None:
    """WebKit scrolls the window, synchronously, when the section's form is
    replaced — tens of pixels, with nothing on the page changing size (#659).
    Chromium never shows it, so only this engine can tell whether the section
    is put back where it was."""
    try:
        browser = browser_factory("webkit")
    except pw.Error:
        pytest.skip("WebKit is not installed (playwright install webkit)")
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(f"{reset_carousel_server}/album/{ALBUM_ID}")
    page.wait_for_selector("#album-artwork .art-pick__picker")
    page.wait_for_function(SETTLED)
    page.locator("#album-artwork .art-pick__picker").scroll_into_view_if_needed()
    page.evaluate("window.scrollBy({top: 200, behavior: 'instant'})")
    before = page.evaluate("scrollY")

    with htmx_complete(page, "album-artwork"):
        page.evaluate(
            """() => htmx.ajax('GET', location.pathname + '/artwork',
                              {target: '#album-artwork', swap: 'innerHTML settle:0ms'})"""
        )

    assert page.evaluate("scrollY") == before
    page.context.close()


def test_the_carousel_goes_on_to_another_release(
    browser_factory, reset_carousel_server: str
) -> None:
    """Arriving at the last image lists the next release, so › past it is
    instant — and Use sends whichever image is shown."""
    _browser, page = _open(browser_factory, reset_carousel_server)
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
    page.context.close()


def test_the_carousel_offers_no_step_past_either_end(
    browser_factory, reset_carousel_server: str
) -> None:
    """‹ and › only where they go somewhere, as the rows' ↑ ↓ stop at the
    ends: no ‹ on the first image, and no › on the last once no release can
    follow it — where it was offered and did nothing. Drawn disabled there,
    not hidden, so the one left reads as one of a pair (#918)."""
    _browser, page = _open(browser_factory, reset_carousel_server)
    section = page.locator("#album-artwork")
    back = section.locator(".art-pick__picker .art-pick__turn--back")
    on = section.locator(".art-pick__picker .art-pick__turn--on")

    pw.expect(back).to_be_visible()
    pw.expect(back).to_be_disabled()  # 101, the first
    pw.expect(on).to_be_enabled()
    with page.expect_response(lambda r: "more=ahead" in r.url):
        on.click()
    page.wait_for_function(SETTLED)
    # 103, the album's last image — but another release may follow it.
    pw.expect(back).to_be_enabled()
    pw.expect(on).to_be_enabled()

    on.click()  # 201, the other release's only image, and the group's last
    assert _shown(page) == ["201"]
    pw.expect(on).to_be_visible()
    pw.expect(on).to_be_disabled()
    pw.expect(back).to_be_enabled()
    page.context.close()


def test_a_late_archive_check_leaves_the_picker_where_it_was_moved(
    browser_factory,
    reset_carousel_server: str,
) -> None:
    """Moving the picker makes no request, so a check sent before the move
    answers for where the picker WAS — and swapped in as served, it put the
    picker back there: the picker jumped, apparently at random (#659)."""
    _browser, page = _open(browser_factory, reset_carousel_server)
    section = page.locator("#album-artwork")
    held = []

    def hold(route):
        held.append((route, route.fetch()))
        page.evaluate("window.__archiveHeld = true")

    page.route("**/artwork?reread=1*", hold)
    page.locator('[hx-target="#album-artwork"][hx-get$="?reread=1"]').click()
    page.wait_for_function("() => document.querySelector('.htmx-request')")

    section.locator(".art-row").nth(1).locator(".art-row__select").click()
    section.get_by_role("checkbox", name="Show Front covers only").uncheck()
    section.get_by_role("button", name="Next image").click()  # 101 → 102, the back
    page.wait_for_function("window.__archiveHeld === true")
    route, response = held.pop()
    with page.expect_response(lambda r: "reread=1" in r.url):
        route.fulfill(response=response)
    page.wait_for_function(SETTLED)

    checked = section.locator("input[name='row']:checked")
    pw.expect(checked).to_have_value(
        section.locator("input[name='row']").nth(1).get_attribute("value") or ""
    )
    assert _shown(page) == ["102"]
    pw.expect(section.get_by_role("checkbox", name="Show Front covers only")).not_to_be_checked()
    page.context.close()


def test_next_past_the_last_image_steps_when_nothing_looked_ahead(
    browser_factory,
    reset_carousel_server: str,
) -> None:
    """Without a look ahead in hand — it failed, or was still on its way —
    › on the last image asks for the next release itself, and shows it."""
    _browser, page = _open(browser_factory, reset_carousel_server)
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
    page.context.close()
