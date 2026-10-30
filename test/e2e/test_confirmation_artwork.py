"""Release confirmation scopes artwork from both of its hosts (#483)."""

from __future__ import annotations

import os
import re
from urllib.parse import parse_qs

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")


def test_review_outcome_matches_the_images_written(
    browser_factory, confirmation_outcome_server, htmx_ready
):
    from harmonist import demo, formats, images

    base, scenario, root = confirmation_outcome_server
    paths = sorted(
        p
        for p in root.rglob("*")
        if formats.is_supported(p) and formats.read_album_id(p) == "demo-rel-dingoes"
    )
    assert len(paths) == 3
    before = {p: formats.read_cover(p) for p in paths}
    cover = paths[0].parent / "cover.png"
    old_cover = cover.read_bytes() if cover.exists() else None
    selected = (
        demo.ASSETS_DIR / ("dingoes.png" if scenario == "identical" else "wyld.png")
    ).read_bytes()
    included = scenario in {"same-size-on", "protected-folder", "tracks-only"}
    writes = scenario not in {"identical", "protected"}
    browser = browser_factory("chromium")
    page = browser.new_page()
    page.goto(f"{base}/album/demo-rel-dingoes")
    use = (
        page.locator("#contribution-editions-demo-rel-dingoes")
        .get_by_role("listitem", name="Suggested digital release")
        .get_by_role("button", name="Use", exact=True)
    )
    htmx_ready(use).click()
    review = page.get_by_role("region", name="Review suggested release")
    artwork = review.locator(".assignment-artwork")
    keep = artwork.get_by_text("Keep existing artwork", exact=True)
    if writes:
        pw.expect(keep).to_have_count(0)
    else:
        pw.expect(keep).to_be_visible()
    candidate = artwork.get_by_alt_text("Artwork for selected release", exact=True)
    identical = artwork.get_by_text("Identical to existing artwork")

    def beside(element, label):
        current = artwork.get_by_alt_text(f"Current artwork: {label}", exact=True)
        assert element.bounding_box()["y"] == pytest.approx(current.bounding_box()["y"], abs=1)

    if scenario == "identical":
        pw.expect(candidate).to_have_count(0)
        beside(identical, "All 3 tracks and cover.png")
    elif scenario == "tracks-only":
        # The folder cover is the selected release's image; the tracks'
        # differs, and the candidate stands beside what it would replace.
        beside(identical, "cover.png")
        beside(candidate, "All 3 tracks")
    else:
        pw.expect(candidate).to_be_visible()
        pw.expect(identical).to_have_count(0)
        if writes:
            beside(
                candidate,
                "Track 1 and cover.png"
                if scenario == "protected-folder"
                else "All 3 tracks and cover.png",
            )
        # These are different pictures despite having the same dimensions.
        original = before[paths[0]]
        assert original is not None
        assert images.dimensions(selected) == images.dimensions(original[0])
    checkbox = artwork.get_by_role("checkbox", name="Use artwork from the selected release")
    if writes:
        pw.expect(checkbox).not_to_be_checked()
        will_update = artwork.get_by_text("Will update:", exact=False)
        pw.expect(will_update).to_be_hidden()
        checkbox.check()
        pw.expect(will_update).to_be_visible()
        expected = (
            "folder cover (cover.png)"
            if scenario == "protected-folder"
            else "embedded artwork in 3 tracks"
            if scenario == "tracks-only"
            else "embedded artwork in 3 tracks and folder cover (cover.png)"
        )
        pw.expect(will_update).to_have_text(f"Will update: {expected}.")
        with page.expect_response(lambda r: "/artwork?" in r.url and "reread=true" in r.url):
            artwork.get_by_role(
                "button", name="Ask the Cover Art Archive about this release again"
            ).click()
        pw.expect(checkbox).to_be_checked()
        pw.expect(will_update).to_be_visible()
        checkbox.set_checked(included)
        pw.expect(will_update).to_be_visible(visible=included)
    else:
        pw.expect(checkbox).to_have_count(0)
    if scenario.startswith("protected"):
        pw.expect(artwork).to_contain_text("Differing per-track artwork is preserved")
    confirm = review.get_by_role("button", name="Confirm suggestion", exact=True)
    pw.expect(confirm).to_have_count(1)
    with page.expect_response(lambda r: "/confirm/" in r.url and "/accept" in r.url) as response:
        confirm.click()
    assert "confirmation-applied" in response.value.headers.get("hx-trigger", "")
    page.wait_for_url("**/album/demo-rel-dingoes-digital*")
    pw.expect(page.locator("#album-artwork")).to_be_visible()
    for path in paths:
        assert formats.read_album_id(path) == "demo-rel-dingoes-digital"
        assert formats.read_cover(path) == (
            (selected, "image/png") if included and scenario != "protected-folder" else before[path]
        )
    assert (cover.read_bytes() if cover.exists() else None) == (selected if included else old_cover)
    page.context.close()


def _suggest(page, base):
    headers = {"HX-Request": "true"}
    assert page.request.post(f"{base}/library/demo-rel-dingoes/rematch", headers=headers).ok
    page.goto(base)
    # Let the card's automatic candidate lookup finish before choosing a
    # different release, so that response cannot overwrite the test's choice.
    page.wait_for_load_state("networkidle")
    card = page.locator('div[id^="task-"].relative').filter(
        has_text="Little Bit o' Hoot, Whole Lotta Nanny"
    )
    pw.expect(card).to_have_count(1)
    aid = card.get_attribute("id").removeprefix("task-")
    assert page.request.post(
        f"{base}/manual/{aid}/assign", headers=headers, form={"mbid": "demo-rel-thamesmen"}
    ).ok
    return aid


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_stale_assignment_response_preserves_current_artwork(
    browser_factory, reset_demo_server, engine
):
    browser = browser_factory(engine)
    page = browser.new_page()
    aid = _suggest(page, reset_demo_server)
    page.goto("about:blank")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script(
        """(() => {
            const open = XMLHttpRequest.prototype.open;
            const send = XMLHttpRequest.prototype.send;
            let held = false;
            XMLHttpRequest.prototype.open = function(method, url, ...args) {
                this.assignmentRead = url.startsWith('/assignments/ALBUM_ID?');
                return open.call(this, method, url, ...args);
            };
            XMLHttpRequest.prototype.send = function(...args) {
                if (this.assignmentRead && !held) {
                    held = true;
                    const onload = this.onload;
                    this.onload = (...events) => {
                        window.deliverOldAssignment = () => onload.apply(this, events);
                        window.assignmentResponseHeld = true;
                    };
                }
                return send.apply(this, args);
            };
        })()""".replace("ALBUM_ID", aid)
    )
    page.goto(reset_demo_server)
    page.wait_for_function("window.assignmentResponseHeld === true")
    page.route(
        "**/tasks",
        lambda route: route.fulfill(
            status=200, content_type="text/html", body="<p>Scanning library…</p>"
        ),
        times=1,
    )
    page.evaluate("htmx.trigger(document.body, 'tasks-changed')")
    pw.expect(page.locator("#task-list")).to_have_text("Scanning library…")
    page.evaluate("htmx.trigger(document.body, 'tasks-changed')")
    artwork = page.locator(f"#task-{aid} .assignment-artwork")
    pw.expect(artwork.get_by_alt_text("Artwork for selected release")).to_be_visible()
    page.evaluate("window.deliverOldAssignment()")
    page.wait_for_load_state("networkidle")
    pw.expect(artwork.get_by_alt_text("Artwork for selected release")).to_be_visible()
    assert not errors
    page.context.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("dismiss", [False, True], ids=["open", "dismissed"])
def test_inbox_refresh_preserves_artwork_inspection(
    browser_factory, artwork_refresh_server, engine, dismiss
):
    base = artwork_refresh_server
    browser = browser_factory(engine)
    page = browser.new_page(viewport={"width": 900, "height": 700})
    aid = _suggest(page, base)
    page.goto(base)
    page.wait_for_load_state("networkidle")
    artwork = page.locator(f"#task-{aid} .assignment-artwork")
    pw.expect(artwork.get_by_alt_text("Artwork for selected release")).to_be_visible()
    thumb = artwork.locator("button[popovertarget]").first
    thumb.click()
    viewer = page.locator(".art-full:popover-open")
    # Inspect a different image, while Escape still belongs to the opener.
    on = viewer.get_by_role("button", name="Next image")
    selected = on.get_attribute("data-art-view")
    on.click()
    pw.expect(viewer).to_have_attribute("id", selected)
    viewer.locator("[data-art-zoom]").click()
    stage = viewer.get_by_role("region", name="Artwork detail")
    stage.evaluate("e => { e.scrollLeft = 200; e.scrollTop = 150; }")
    stage.focus()
    assert stage.evaluate("e => [e.scrollLeft, e.scrollTop]") == [200, 150]
    held = []

    def hold(route):
        held.append((route, route.fetch()))
        page.evaluate("window.inboxResponseHeld = true")

    page.route("**/tasks", hold)
    page.evaluate("""() => {
            window.inboxRefreshes = 0;
            document.body.addEventListener('htmx:afterSettle', e => {
                if (e.target.id === 'task-list') window.inboxRefreshes++;
            });
        }""")
    for refresh in range(2):
        page.evaluate("""() => {
                window.inboxResponseHeld = false;
                htmx.trigger(document.body, 'tasks-changed');
            }""")
        page.wait_for_function("window.inboxResponseHeld === true")
        if dismiss and refresh == 0:
            page.keyboard.press("Escape")
            pw.expect(viewer).to_have_count(0)
        route, response = held.pop()
        route.fulfill(response=response)
        page.wait_for_function("n => window.inboxRefreshes > n", arg=refresh)
        if dismiss:
            pw.expect(viewer).to_have_count(0)
        else:
            pw.expect(viewer).to_have_attribute("id", selected)
            pw.expect(viewer.locator("[data-art-zoom]")).to_have_attribute("aria-pressed", "true")
            pw.expect(stage).to_be_focused()
            assert stage.evaluate("e => [e.scrollLeft, e.scrollTop]") == [200, 150]
    if not dismiss:
        page.keyboard.press("Escape")
        pw.expect(viewer).to_have_count(0)
        pw.expect(thumb).to_be_focused()
    page.context.close()


@pytest.mark.parametrize("on_album", [False, True])
def test_review_artwork_inspection_and_refresh_preserve_draft(
    browser_factory, reset_demo_server, on_album
):
    base = reset_demo_server
    browser = browser_factory("chromium")
    page = browser.new_page()
    aid = _suggest(page, base)
    page.goto(f"{base}/album/{aid}" if on_album else base)
    # The editor's host differs between the Inbox and album page.
    review = (page.locator("main") if on_album else page.locator(f"#task-{aid}")).locator(
        ".assignment-editor"
    )
    artwork = review.locator(".assignment-artwork")
    pw.expect(artwork.get_by_alt_text("Artwork for selected release")).to_be_visible()
    if on_album:
        pw.expect(page.locator("#album-artwork")).to_be_hidden()
    thumbs = artwork.locator("button[popovertarget]")
    assert thumbs.count() >= 2, "current and selected artwork must both open full size"
    # Measure one live DOM snapshot. Resolving each thumbnail separately
    # can retain a detached node across an Inbox morph and report 0 x 0.
    sizes = thumbs.evaluate_all("els => els.map(e => [e.offsetWidth, e.offsetHeight])")
    assert all(width > 0 and height > 0 for width, height in sizes)
    assert all(size == sizes[0] for size in sizes)
    for thumb in thumbs.all():
        target = thumb.get_attribute("popovertarget")
        full = page.locator(f'[id="{target}"]')
        pw.expect(full).to_have_count(1)
        pw.expect(full.locator("img")).to_have_attribute(
            "src", thumb.locator("img").get_attribute("src")
        )
        pw.expect(thumb.locator("img")).to_have_css("object-fit", "contain")
        thumb.click()
        pw.expect(full).to_be_visible()
        assert full.evaluate("e => e.matches(':popover-open')")
        page.keyboard.press("Escape")
        pw.expect(full).not_to_be_visible()
    # Other Inbox cards may show the same image: each target still belongs
    # to exactly one view, even when several buttons share it.
    for target in page.locator("button[popovertarget]").evaluate_all(
        "els => els.map(e => e.getAttribute('popovertarget'))"
    ):
        pw.expect(page.locator(f'[id="{target}"]')).to_have_count(1)

    checkbox = artwork.get_by_role("checkbox", name="Use artwork from the selected release")
    checkbox.check()
    review.get_by_role("button", name="Edit track assignments").click()
    review.get_by_role("button", name="Move on-disk entry down", exact=True).first.click()
    pw.expect(review.locator('[name="disk_order"]')).to_have_value("1,0,2")
    fingerprint = review.locator('[name="release_fingerprint"]').input_value()
    timestamp = artwork.locator('[title^="Cover Art Archive last asked"]')
    pw.expect(timestamp).to_be_visible()
    # Distinguish the replaced date from a stale date surviving the swap.
    timestamp.evaluate("e => e.textContent = 'previous check'")
    with page.expect_response(
        lambda r: f"/assignments/{aid}/artwork?" in r.url and "reread" in r.url
    ) as refreshed:
        artwork.get_by_role(
            "button", name="Ask the Cover Art Archive about this release again"
        ).click()
    assert refreshed.value.ok
    assert parse_qs(refreshed.value.url.split("?", 1)[1])["release_fingerprint"] == [fingerprint]
    pw.expect(timestamp).to_have_text("just now")
    pw.expect(artwork).to_contain_text("CAA checked")
    pw.expect(checkbox).to_be_checked()
    pw.expect(review.locator('[name="disk_order"]')).to_have_value("1,0,2")
    pw.expect(review.get_by_role("button", name="Accept changes", exact=True)).to_be_visible()
    page.context.close()


@pytest.mark.parametrize("on_album", [False, True])
@pytest.mark.parametrize("included", [False, True])
def test_confirmation_applies_only_included_artwork(
    browser_factory, reset_demo_server, on_album, included
):
    base = reset_demo_server
    browser = browser_factory("chromium")
    page = browser.new_page()
    page.on("dialog", lambda dialog: dialog.accept())
    aid = _suggest(page, base)
    page.goto(f"{base}/album/{aid}" if on_album else base)
    host = page.locator("main") if on_album else page.locator(f"#task-{aid}")
    review = host.locator(".assignment-editor")
    pw.expect(review).to_be_visible()
    candidate = review.get_by_alt_text("Artwork for selected release")
    pw.expect(candidate).to_be_visible()
    page.wait_for_function(
        "() => document.querySelector('.assignment-editor img[alt=\"Artwork for selected release\"]').naturalWidth > 0"
    )
    candidate_digest = candidate.get_attribute("src").rsplit("/", 1)[-1]
    before = review.locator('img[alt^="Current artwork"]').evaluate_all(
        "els => els.map(e => e.src.split('/').pop())"
    )
    checkbox = review.get_by_role("checkbox", name="Use artwork from the selected release")
    pw.expect(checkbox).not_to_be_checked()
    checkbox.set_checked(included)
    review.get_by_role("button", name="Edit track assignments").click()
    review.get_by_role("button", name="Move on-disk entry down", exact=True).first.click()
    pw.expect(review.locator('[name="disk_order"]')).to_have_value("1,0,2")
    review.get_by_role("button", name="Reset", exact=True).click()
    pw.expect(review.locator('[name="disk_order"]')).to_have_value("0,1,2")
    review.locator('[name="assignment_action"][value="cancel"]').click()
    pw.expect(checkbox).to_be_checked(checked=included)
    with page.expect_response(lambda r: r.url.endswith(f"/confirm/{aid}/accept")) as confirmed:
        review.get_by_role("button", name="Confirm suggestion", exact=True).click()
    assert "confirmation-applied" in confirmed.value.headers.get("hx-trigger", "")
    assert (
        parse_qs(confirmed.value.request.post_data)["include_artwork"][-1] == str(included).lower()
    )
    if on_album:
        page.wait_for_url(f"{base}/album/demo-rel-thamesmen")
    else:
        pw.expect(page.get_by_role("dialog")).not_to_be_visible()
        page.goto(f"{base}/album/demo-rel-thamesmen")
    page.wait_for_selector("#album-artwork .art-row")
    page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")
    current = page.locator(
        "#album-artwork .art-row__side:not(.art-row__side--after) img"
    ).evaluate_all("els => els.map(e => e.src.split('/').pop())")
    assert current
    assert set(current) == ({candidate_digest} if included else set(before))
    if not included:
        pw.expect(page.locator("#album-artwork button.art-pick__use:visible")).to_be_visible()
    page.context.close()


def test_pending_artwork_does_not_block_tags_only_acceptance(browser_factory, reset_demo_server):
    base = reset_demo_server
    browser = browser_factory("chromium")
    page = browser.new_page()
    page.on("dialog", lambda dialog: dialog.accept())
    aid = _suggest(page, base)
    # Leave the Inbox first, so its refresh after the assign cannot be held.
    page.goto("about:blank")
    pending = []
    page.route("**/assignments/*/artwork?*", lambda route: pending.append(route))
    page.goto(base)
    review = page.locator(f"#task-{aid} .assignment-editor")
    pw.expect(review).to_contain_text("Loading artwork")
    with page.expect_response(lambda r: r.url.endswith(f"/confirm/{aid}/accept")) as applied:
        review.get_by_role("button", name="Confirm suggestion", exact=True).click()
    assert pending
    assert parse_qs(applied.value.request.post_data)["include_artwork"] == ["false"]
    assert "confirmation-applied" in applied.value.headers.get("hx-trigger", "")
    for route in pending:
        route.abort()
    page.context.close()


@pytest.mark.parametrize("width", [1280, 650, 390])
@pytest.mark.parametrize("on_album", [False, True])
def test_current_artwork_keeps_actions_in_place_while_loading(
    browser_factory, reset_public_demo_server, width, on_album
):
    browser = browser_factory("chromium")
    page = browser.new_page(viewport={"width": width, "height": 1000})
    base = reset_public_demo_server
    aid = _suggest(page, base)
    # Leave the Inbox first: the assign refreshes it, and its card's own
    # artwork request could otherwise be held, mid-navigation (#828).
    page.goto("about:blank")
    pending = []

    def hold(route):
        pending.append(route)
        page.evaluate("window.artworkRequestHeld = true")

    page.route(f"**/assignments/{aid}/artwork?*", hold)
    page.goto(f"{base}/album/{aid}" if on_album else base)
    host = page.locator("main") if on_album else page.locator(f"#task-{aid}")
    review = host.locator(".assignment-editor")
    artwork = review.locator(".assignment-artwork")
    pw.expect(artwork).to_contain_text("Loading artwork")
    current = artwork.locator('img[alt^="Current artwork:"]')
    pw.expect(current.first).to_be_visible()
    pw.expect(current.first).to_have_js_property("complete", True)
    assert current.first.evaluate("e => e.naturalWidth") > 0
    # Document coordinates avoid confusing browser scroll anchoring with
    # actual layout stability, even when the controls are below the fold.
    actions = review.get_by_role("button", name="Confirm suggestion", exact=True)
    before = actions.evaluate("e => e.getBoundingClientRect().top + window.scrollY")
    thumb = artwork.locator("button[popovertarget]").first
    thumb.focus()
    page.keyboard.press("Enter")
    viewer = page.locator(f"#{thumb.get_attribute('popovertarget')}")
    pw.expect(viewer).to_be_visible()
    # At 100% where the image is larger than the window; where it already
    # fits, there is no other scale to keep.
    zoom = viewer.locator("[data-art-zoom]")
    pw.expect(viewer.locator(".art-full__stage")).to_have_attribute("data-zoom", re.compile("."))
    native = "false" if zoom.is_disabled() else "true"
    if native == "true":
        zoom.click()
    pw.expect(zoom).to_have_attribute("aria-pressed", native)
    # Current artwork is usable before HTMX starts the archive request.
    # Wait for the request we will release, not for unrelated UI work to
    # happen to give its load trigger enough time on a busy runner.
    page.wait_for_function("window.artworkRequestHeld === true")
    assert pending
    for route in pending:
        route.continue_()
    pw.expect(artwork.get_by_alt_text("Artwork for selected release")).to_be_visible()
    pw.expect(artwork).to_contain_text("CAA checked")
    pw.expect(viewer).to_be_visible()
    pw.expect(zoom).to_have_attribute("aria-pressed", native)
    page.keyboard.press("Escape")
    pw.expect(thumb).to_be_focused()
    after = actions.evaluate("e => e.getBoundingClientRect().top + window.scrollY")
    assert after == pytest.approx(before, abs=1)
    page.context.close()


def test_changed_confirmation_preserves_review_and_artwork_choice(
    browser_factory, reset_demo_server, htmx_ready
):
    base = reset_demo_server
    browser = browser_factory("chromium")
    page = browser.new_page()
    page.on("dialog", lambda dialog: dialog.accept())
    aid = _suggest(page, base)
    page.goto(base)
    card = page.locator(f"#task-{aid}")
    pw.expect(card.locator(".assignment-artwork")).to_contain_text("CAA checked")
    page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")
    card.get_by_role("button", name="Edit track assignments").click()
    editor = card.locator(".assignment-editor")
    pw.expect(editor.get_by_role("button", name="Accept changes")).to_be_visible()
    editor.get_by_role("button", name="Accept changes").click()
    pw.expect(editor.get_by_role("button", name="Edit track assignments")).to_be_visible()
    checkbox = editor.get_by_role("checkbox", name="Use artwork from the selected release")
    checkbox.check()
    draft = editor.locator('[name="disk_order"]').input_value()
    # A stale explicit pairing must be reviewed again before it can be saved.
    fingerprint = editor.locator('[name="release_fingerprint"]').input_value()
    editor.locator('[name="release_fingerprint"]').evaluate(
        "e => e.value = e.value.split(':')[0] + ':obsolete'"
    )
    editor.get_by_role("button", name="Confirm suggestion").click()
    pw.expect(editor.get_by_role("alert")).to_contain_text("Refresh the comparison")
    pw.expect(editor.locator('[name="disk_order"]')).to_have_value(draft)
    pw.expect(checkbox).to_be_checked()
    pw.expect(editor.get_by_role("button", name="Confirm suggestion")).to_be_enabled()
    with page.expect_response(lambda r: f"/assignments/{aid}?" in r.url and "reread" in r.url):
        card.get_by_role(
            "button",
            name="Read this release from MusicBrainz again and reset assignment changes",
        ).click()
    pw.expect(editor.locator('[name="release_fingerprint"]')).to_have_value(fingerprint)
    with page.expect_response(lambda r: r.url.endswith(f"/confirm/{aid}/accept")) as applied:
        htmx_ready(card.get_by_role("button", name="Confirm suggestion", exact=True)).click()
    assert "confirmation-applied" in applied.value.headers.get("hx-trigger", "")
    page.context.close()
