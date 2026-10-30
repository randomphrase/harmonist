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


def _open(browser_factory, base: str):
    browser = browser_factory("chromium")
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
    pw.expect(panel.locator(".art-pick__nav .art-pick__count")).to_have_text("2 of 3")
    pw.expect(panel.locator(".art-pick__row-count")).to_have_text(re.compile(r"^2 of \d+$"))

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
    follow it — where it was offered and did nothing."""
    _browser, page = _open(browser_factory, reset_carousel_server)
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
    section.get_by_role("checkbox", name="Front only").uncheck()
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
    pw.expect(section.get_by_role("checkbox", name="Front only")).not_to_be_checked()
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
