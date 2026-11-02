"""Arrows, draft preservation and confirmation must work in a real browser."""

import os
import re
from urllib.parse import parse_qs

import pytest
from test.e2e.conftest import open_review

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")


def _opened(page, host):
    review = open_review(page, host)
    pw.expect(review.locator(".assignment-artwork")).to_contain_text("CAA checked")
    page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")
    return review


def _review(page, base):
    """The demo's suggestion, with its review open (#933): (album id, dialog)."""
    page.goto(base)
    card = page.locator('div[id^="task-"].relative').filter(has_text="Gimme Some Money")
    pw.expect(card).to_have_count(1)
    return card.get_attribute("id").removeprefix("task-"), _opened(page, card)


@pytest.mark.parametrize("on_album", [False, True])
def test_suggested_release_link_follows_review_and_confirmation(
    browser_factory, reset_demo_server, on_album
):
    base = reset_demo_server
    browser = browser_factory("chromium")
    page = browser.new_page(viewport={"width": 1280, "height": 1000})
    page.on("dialog", lambda dialog: dialog.accept())
    aid, review = _review(page, base)
    host = page.locator("#album-inbox-actions") if on_album else page.locator(f"#task-{aid}")
    if on_album:
        page.goto(f"{base}/album/{aid}")
        _opened(page, host)
    heading = review.get_by_role("heading", name=re.compile("Suggested match:"))
    link = heading.get_by_role("link")
    pw.expect(link).to_have_attribute("href", "https://musicbrainz.org/release/demo-rel-thamesmen")
    pw.expect(
        review.locator('a[href="https://musicbrainz.org/release/demo-rel-thamesmen"]')
    ).to_have_count(1)
    edit = review.get_by_role("button", name="Edit track assignments", exact=True)
    pw.expect(edit).to_be_visible()
    pw.expect(edit).not_to_have_class(re.compile("bg-amber-50"))
    checked = review.get_by_text("MB checked", exact=True)
    groups = review.locator('th[scope="colgroup"]')
    pw.expect(groups.nth(0)).to_contain_text("On disk · 3 files")
    pw.expect(groups.nth(1)).to_contain_text("MusicBrainz")
    pw.expect(groups.nth(1)).to_contain_text("3 tracks")
    pw.expect(groups.get_by_text(re.compile(r"file.*unassigned"))).to_have_count(0)
    pw.expect(groups.nth(1).get_by_role("button", name="Edit track assignments")).to_be_visible()
    assert checked.bounding_box()["y"] < edit.bounding_box()["y"]
    assert abs(groups.nth(0).bounding_box()["y"] - edit.bounding_box()["y"]) < 12
    pw.expect(review.locator(".assignment-artwork")).to_contain_text("CAA checked")
    columns = review.locator("thead tr").last.locator("th")
    positions = columns.evaluate_all("els => els.map(e => e.getBoundingClientRect().x)")
    delta = review.locator('[title="Duration difference"]').first
    assert delta.evaluate("""e => {
            const r = document.createRange();
            r.selectNodeContents(e.parentElement.firstChild);
            return Math.abs(r.getBoundingClientRect().y - e.getBoundingClientRect().y) < 5;
        }""")
    edit.click()
    pw.expect(review.get_by_role("button", name="Accept changes")).to_be_visible()
    accept = review.get_by_role("button", name="Accept changes", exact=True)
    confirm = review.get_by_role("button", name="Confirm suggestion", exact=True)
    dismiss = review.get_by_role("button", name="Dismiss suggestion", exact=True)
    artwork = review.locator(".assignment-artwork")
    for action in (confirm, dismiss):
        pw.expect(action).to_be_visible()
        pw.expect(action).to_be_disabled()
    boxes = (
        review.locator("table")
        .or_(accept)
        .or_(artwork)
        .or_(confirm)
        .or_(dismiss)
        .evaluate_all("""elements => Object.fromEntries(elements.map(element => [
                element.matches('table') ? 'table' :
                    element.matches('.assignment-artwork') ? 'artwork' : element.textContent.trim(),
                element.getBoundingClientRect().toJSON()
            ]))""")
    )
    assert len(boxes) == 5
    assert all(box["height"] > 0 for box in boxes.values())
    assert boxes["table"]["bottom"] <= boxes["Accept changes"]["y"]
    assert boxes["Accept changes"]["y"] < boxes["artwork"]["y"]
    for label in ("Confirm suggestion", "Dismiss suggestion"):
        assert boxes[label]["y"] >= boxes["artwork"]["bottom"]
    pw.expect(link).to_be_visible()
    # Colspan content can change subpixel table rounding between modes.
    assert columns.evaluate_all(
        "els => els.map(e => e.getBoundingClientRect().x)"
    ) == pytest.approx(positions, abs=1)
    review.get_by_role("button", name="Cancel", exact=True).click()
    pw.expect(edit).to_be_visible()
    pw.expect(confirm).to_be_enabled()
    pw.expect(dismiss).to_be_enabled()
    pw.expect(link).to_be_visible()

    # Replace the candidate through the application's normal assignment path.
    assert page.request.post(
        f"{base}/manual/{aid}/assign",
        headers={"HX-Request": "true"},
        form={"mbid": "demo-rel-folksmen"},
    ).ok
    page.reload()
    open_review(page, host)
    pw.expect(link).to_have_attribute("href", "https://musicbrainz.org/release/demo-rel-folksmen")
    pw.expect(edit).to_have_class(re.compile("bg-amber-50"))
    pw.expect(groups.nth(0)).to_contain_text("1 file unassigned")
    pw.expect(review.locator(".assignment-artwork")).to_contain_text("CAA checked")
    page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")
    pw.expect(review).to_contain_text("Unassigned files will receive the album's MusicBrainz ID")
    with page.expect_response(lambda r: r.url.endswith(f"/confirm/{aid}/accept")) as applied:
        review.get_by_role("button", name="Confirm suggestion", exact=True).click()
    assert "confirmation-applied" in applied.value.headers.get("hx-trigger", "")
    if on_album:
        page.wait_for_url(f"{base}/album/demo-rel-folksmen")
    page.goto(f"{base}/album/demo-rel-folksmen")
    # The page's own release link, by its id: History now links the release
    # it was tagged to as well (#801), so a match on the URL alone finds two.
    pw.expect(page.locator("#album-release-demo-rel-folksmen")).to_have_attribute(
        "href", "https://musicbrainz.org/release/demo-rel-folksmen"
    )
    page.context.close()


@pytest.mark.parametrize("engine_name", ["chromium", "webkit"])
def test_inbox_refresh_cannot_discard_assignment_reset(
    browser_factory, reset_demo_server, engine_name
):
    browser = browser_factory(engine_name)
    page = browser.new_page()
    aid, review = _review(page, reset_demo_server)
    review.get_by_role("button", name="Edit track assignments").click()
    editor = review.locator(".assignment-editor")
    editor.get_by_role("button", name="Move on-disk entry down", exact=True).first.click()
    pw.expect(editor.locator('[name="disk_order"]')).to_have_value("1,0,2")
    held = []

    def hold(route):
        held.append((route, route.fetch()))
        page.evaluate("window.resetResponseHeld = true")

    page.route(f"**/assignments/{aid}", hold)
    editor.get_by_role("button", name="Reset", exact=True).click()
    page.wait_for_function("window.resetResponseHeld === true")
    page.evaluate("""() => {
            window.inboxStarted = false;
            window.inboxSettled = false;
            document.body.addEventListener('htmx:beforeRequest', e => {
                if (e.target.id === 'task-list') window.inboxStarted = true;
            });
            document.body.addEventListener('htmx:afterSettle', e => {
                if (e.target.id === 'task-list') window.inboxSettled = true;
            });
            htmx.trigger(document.body, 'tasks-changed');
        }""")
    # Without synchronization, deliberately land the enclosing refresh
    # first. With it, the refresh waits for Reset and then preserves its result.
    if page.evaluate("window.inboxStarted"):
        page.wait_for_function("window.inboxSettled")
    route, response = held.pop()
    route.fulfill(response=response)
    page.wait_for_function("window.inboxSettled")
    pw.expect(editor.locator('[name="disk_order"]')).to_have_value("0,1,2")
    pw.expect(editor.get_by_role("button", name="Reset", exact=True)).to_be_enabled()
    page.context.close()


@pytest.mark.parametrize("engine_name", ["chromium", "webkit"])
def test_assignment_reset_preempts_inflight_inbox_refresh(
    browser_factory, reset_demo_server, engine_name
):
    browser = browser_factory(engine_name)
    page = browser.new_page()
    _, review = _review(page, reset_demo_server)
    review.get_by_role("button", name="Edit track assignments").click()
    editor = review.locator(".assignment-editor")
    editor.get_by_role("button", name="Move on-disk entry down", exact=True).first.click()
    pw.expect(editor.locator('[name="disk_order"]')).to_have_value("1,0,2")
    held = []

    def hold(route):
        held.append((route, route.fetch()))
        page.evaluate("window.inboxResponseHeld = true")

    page.route("**/tasks", hold, times=1)
    page.evaluate("""() => {
            window.inboxSettled = false;
            document.body.addEventListener('htmx:afterSettle', e => {
                if (e.target.id === 'task-list') window.inboxSettled = true;
            });
        }""")
    page.evaluate("htmx.trigger(document.body, 'tasks-changed')")
    page.wait_for_function("window.inboxResponseHeld === true")
    # A status update can queue another refresh behind the held read.
    # Reset must take priority over both, without losing its submission.
    page.evaluate("htmx.trigger(document.body, 'tasks-changed')")
    editor.get_by_role("button", name="Reset", exact=True).click()
    route, response = held.pop()
    route.fulfill(response=response)
    page.wait_for_function("window.inboxSettled")
    pw.expect(editor.locator('[name="disk_order"]')).to_have_value("0,1,2")
    pw.expect(editor.get_by_role("button", name="Reset", exact=True)).to_be_enabled()
    page.context.close()


def _refresh_inbox(page):
    page.evaluate("""() => {
        window.inboxSettled = false;
        document.body.addEventListener('htmx:afterSettle', function settled(e) {
            if (e.target.id !== 'task-list') return;
            window.inboxSettled = true;
            document.body.removeEventListener('htmx:afterSettle', settled);
        });
        htmx.trigger(document.body, 'tasks-changed');
    }""")
    page.wait_for_function("window.inboxSettled")


@pytest.mark.parametrize("engine_name", ["chromium", "webkit"])
def test_inbox_refresh_keeps_the_preserved_editor_and_its_focus(
    browser_factory, reset_demo_server, engine_name
):
    browser = browser_factory(engine_name)
    page = browser.new_page()
    _, review = _review(page, reset_demo_server)
    editor = review.locator(".assignment-editor")
    edit = editor.get_by_role("button", name="Edit track assignments", exact=True)
    edit.focus()
    editor.evaluate("el => { el.dataset.probe = ''; window.editorBefore = el; }")
    _refresh_inbox(page)
    assert editor.evaluate("el => el === window.editorBefore")
    pw.expect(edit).to_be_focused()
    page.context.close()


@pytest.mark.parametrize("engine_name", ["chromium", "webkit"])
def test_inbox_refresh_during_an_editor_settle_leaves_it_ready(
    browser_factory, reset_demo_server, htmx_ready, engine_name
):
    browser = browser_factory(engine_name)
    page = browser.new_page()
    _, review = _review(page, reset_demo_server)
    editor = review.locator(".assignment-editor")
    # Widen htmx's 20 ms settle so the refresh reliably lands inside it.
    page.evaluate("htmx.config.defaultSettleDelay = 1500")
    reread = "Read this release from MusicBrainz again and reset assignment changes"
    with page.expect_response(lambda r: "reread=true" in r.url):
        editor.get_by_role("button", name=reread, exact=True).click()
    pw.expect(editor.locator(".htmx-added")).not_to_have_count(0)
    _refresh_inbox(page)
    page.evaluate("htmx.config.defaultSettleDelay = 20")
    edit = editor.get_by_role("button", name="Edit track assignments", exact=True)
    pw.expect(edit).to_be_visible()
    pw.expect(htmx_ready(edit)).to_be_visible(timeout=5_000)
    page.context.close()


@pytest.mark.parametrize("engine_name", ["chromium", "webkit"])
def test_inbox_refresh_during_an_editor_load_reads_it_once(
    browser_factory, reset_demo_server, engine_name
):
    """Refreshes of the Inbox under an open review's load don't fire it again (#821).

    The review is in a dialog now (#933), outside the card a refresh re-renders;
    this keeps that so even when the card itself goes and comes back."""
    browser = browser_factory(engine_name)
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(reset_demo_server)
    card = page.locator('div[id^="task-"].relative').filter(has_text="Gimme Some Money")
    aid = card.get_attribute("id").removeprefix("task-")
    held = []

    def hold(route):
        held.append((route, route.fetch()))
        page.evaluate("window.editorReadHeld = true")

    page.route(f"**/assignments/{aid}?cancel=true*", hold)
    review = open_review(page, card)
    page.wait_for_function("window.editorReadHeld === true")
    # A reconcile renders the Inbox without cards; the next refresh brings
    # this one back.
    page.route(
        "**/tasks",
        lambda route: route.fulfill(
            status=200, content_type="text/html", body="<p>Sorting your library</p>"
        ),
        times=1,
    )
    page.evaluate("htmx.trigger(document.body, 'tasks-changed')")
    pw.expect(page.locator("#task-list")).to_have_text("Sorting your library")
    _refresh_inbox(page)
    _refresh_inbox(page)
    for route, response in held:
        route.fulfill(response=response)
    pw.expect(review.locator(".assignment-artwork")).to_contain_text("CAA checked")
    assert len(held) == 1
    assert not errors
    page.context.close()


def test_column_display_toggle_survives_moves_and_reset(browser_factory, reset_demo_server):
    browser = browser_factory("chromium")
    page = browser.new_page()
    _, review = _review(page, reset_demo_server)
    review.get_by_role("button", name="Edit track assignments").click()
    editor = review.locator(".assignment-editor")
    filenames = editor.get_by_role("radio", name="Filename", exact=True)
    pw.expect(editor.locator("[data-assignment-row] .assignment-title").first).to_be_visible()
    pw.expect(
        editor.locator("[data-assignment-row] .assignment-filename").first
    ).not_to_be_visible()
    filenames.focus()
    filenames.press("Space")
    pw.expect(editor.locator("[data-assignment-row] .assignment-filename").first).to_be_visible()
    pw.expect(editor.locator("[data-assignment-row] .assignment-title").first).not_to_be_visible()
    editor.get_by_role("button", name="Move on-disk entry down", exact=True).first.click()
    pw.expect(editor.locator('[name="disk_order"]')).to_have_value("1,0,2")
    pw.expect(filenames).to_be_checked()
    pw.expect(editor.locator("[data-assignment-row] .assignment-filename").first).to_be_visible()
    editor.get_by_role("radio", name="Title", exact=True).press("Space")
    pw.expect(editor.locator("[data-assignment-row] .assignment-title").first).to_be_visible()
    editor.get_by_role("button", name="Reset", exact=True).click()
    pw.expect(editor.locator('[name="disk_order"]')).to_have_value("0,1,2")
    editor.get_by_role("button", name="Cancel", exact=True).click()
    pw.expect(review.get_by_role("button", name="Confirm suggestion", exact=True)).to_be_visible()
    page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")
    # Read-only mode keeps the same table geometry and display preference.
    columns = editor.locator("thead tr").last.locator("th")
    positions = columns.evaluate_all("els => els.map(el => el.getBoundingClientRect().x)")
    filenames.focus()
    filenames.press("Space")
    pw.expect(editor.locator(".assignment-filename").first).to_be_visible()
    editor.get_by_role("button", name="Edit track assignments").click()
    pw.expect(editor.get_by_role("button", name="Accept changes")).to_be_visible()
    pw.expect(filenames).to_be_checked()
    assert columns.evaluate_all(
        "els => els.map(el => el.getBoundingClientRect().x)"
    ) == pytest.approx(positions, abs=1)
    editor.get_by_role("button", name="Cancel", exact=True).click()
    editor.get_by_role(
        "button", name="Read this release from MusicBrainz again and reset assignment changes"
    ).click()
    pw.expect(editor.get_by_role("button", name="Edit track assignments")).to_be_visible()
    pw.expect(editor.get_by_role("button", name="Move on-disk entry down")).to_have_count(0)
    page.context.close()


def test_partial_confirmation_can_be_applied_and_reviewed(browser_factory, reset_demo_server):
    base = reset_demo_server
    browser = browser_factory("chromium")
    page = browser.new_page()
    page.on("dialog", lambda dialog: dialog.accept())
    aid, review = _review(page, base)
    # Three files against a two-track release leaves one file unassigned.
    assert page.request.post(
        f"{base}/manual/{aid}/assign",
        headers={"HX-Request": "true"},
        form={"mbid": "demo-rel-folksmen"},
    ).ok
    page.reload()
    review = _opened(page, page.locator(f"#task-{aid}"))
    with page.expect_response(lambda r: r.url.endswith(f"/confirm/{aid}/accept")) as applied:
        review.get_by_role("button", name="Confirm suggestion", exact=True).click()
    assert "confirmation-applied" in applied.value.headers.get("hx-trigger", "")
    page.goto(f"{base}/album/demo-rel-folksmen")
    pw.expect(page.locator("main")).to_contain_text("Tracks unassigned")
    # Required assignment reviews now open automatically on the album page.
    dialog = page.locator("#album-track-editor")
    pw.expect(dialog).to_be_visible()
    pw.expect(dialog.locator('[name="disk_order"]')).to_have_value("0,1,2")
    dialog.get_by_role("button", name="Move on-disk entry up", exact=True).last.click()
    pw.expect(dialog.locator('[name="disk_order"]')).to_have_value("0,2,1")
    dialog.get_by_role(
        "button", name="Read this release from MusicBrainz again and reset assignment changes"
    ).click()
    page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")
    pw.expect(dialog.locator('[name="disk_order"]')).to_have_value("0,1,2")
    with page.expect_response(
        lambda r: r.url.endswith("/confirm/demo-rel-folksmen/accept")
    ) as applied:
        dialog.get_by_role("button", name="Accept changes").click()
    assert "confirmation-applied" in applied.value.headers.get("hx-trigger", "")
    pw.expect(dialog).to_be_visible()
    pw.expect(dialog.locator('[name="disk_order"]')).to_have_value("0,1,2")
    pw.expect(dialog).to_contain_text("1 file unassigned")
    page.context.close()


def test_arrow_mapping_survives_refresh_review_and_apply(browser_factory, reset_demo_server):
    base = reset_demo_server
    browser = browser_factory("chromium")
    page = browser.new_page()
    page.on("dialog", lambda dialog: dialog.accept())
    aid, review = _review(page, base)
    review.get_by_role("button", name="Edit track assignments").click()
    editor = review.locator(".assignment-editor")
    down = editor.get_by_role("button", name="Move on-disk entry down", exact=True).first
    page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")
    scan_before = page.request.get(f"{base}/scan/status").json()
    assert scan_before["state"] == "done"
    # Status timestamps have second precision; a fast unwanted scan in the
    # same second could otherwise leave an identical status response.
    page.wait_for_function(
        "finished => Date.now() >= Date.parse(finished) + 1100",
        arg=scan_before["finished_at"],
    )
    down.focus()
    down.press("Enter")
    pw.expect(editor.locator('[name="disk_order"]')).to_have_value("1,0,2")
    assert page.request.get(f"{base}/scan/status").json() == scan_before
    row = editor.locator('[data-assignment-row="0"]')
    note = row.locator("td").nth(0).locator('[aria-label^="Track numbering changes"]')
    pw.expect(note).to_have_text("2 → 1")
    assert note.evaluate("el => el.scrollWidth <= el.clientWidth")
    pw.expect(editor.locator(f'[id="move-{aid}-disk-0-down"]')).to_be_focused()
    pw.expect(review.get_by_role("button", name="Confirm suggestion", exact=True)).to_be_disabled()
    # A normal inbox poll must not reset a corrected draft.
    with page.expect_response(lambda r: "/tasks" in r.url):
        page.evaluate("htmx.trigger(document.body, 'tasks-changed')")
    page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")
    pw.expect(editor.locator('[name="disk_order"]')).to_have_value("1,0,2")
    editor.get_by_role("button", name="Accept changes").click()
    pw.expect(editor.get_by_role("button", name="Confirm suggestion")).to_be_enabled()
    pw.expect(editor.locator('[name="disk_order"]')).to_have_value("1,0,2")
    # Reopening preserves the accepted mapping; cancel restores it after edits.
    editor.get_by_role("button", name="Edit track assignments").click()
    editor.get_by_role("button", name="Move on-disk entry down", exact=True).first.click()
    pw.expect(editor.locator('[name="disk_order"]')).to_have_value("0,1,2")
    editor.get_by_role("button", name="Cancel", exact=True).click()
    pw.expect(editor.locator('[name="disk_order"]')).to_have_value("1,0,2")
    # Only final confirmation writes the accepted pairing.
    with page.expect_response(lambda r: r.url.endswith(f"/confirm/{aid}/accept")) as applied:
        editor.get_by_role("button", name="Confirm suggestion").click()
    assert parse_qs(applied.value.request.post_data)["disk_order"] == ["1,0,2"]
    assert "confirmation-applied" in applied.value.headers.get("hx-trigger", "")
    pw.expect(page.get_by_role("dialog")).not_to_be_visible()
    page.context.close()


def test_gap_can_move_on_either_side_and_cancel_discards_the_draft(
    browser_factory, reset_demo_server
):
    base = reset_demo_server
    browser = browser_factory("chromium")
    page = browser.new_page()
    aid, review = _review(page, base)
    # Three local files against the demo's four-track release.
    assert page.request.post(
        f"{base}/manual/{aid}/assign",
        headers={"HX-Request": "true"},
        form={"mbid": "demo-rel-electric-mayhem"},
    ).ok
    page.reload()
    review = _opened(page, page.locator(f"#task-{aid}"))
    review.get_by_role("button", name="Edit track assignments").click()
    editor = review.locator(".assignment-editor")
    pw.expect(editor.locator('[name="disk_order"]')).to_have_value("0,1,2,-")
    editor.get_by_role("button", name="Move on-disk entry up", exact=True).last.click()
    pw.expect(editor.locator('[name="disk_order"]')).to_have_value("0,1,-,2")
    pw.expect(editor.locator(f'[id="move-{aid}-disk-gap-0-up"]')).to_be_focused()
    pw.expect(editor.locator('[data-assignment-row="2"]')).to_contain_text("Missing on disk")
    editor.get_by_role("button", name="Move MusicBrainz entry up", exact=True).last.click()
    pw.expect(editor.locator('[name="mb_order"]')).to_have_value("0,1,3,2")
    editor.get_by_role("button", name="Cancel").click()
    editor.get_by_role("button", name="Edit track assignments").click()
    pw.expect(editor.locator('[name="disk_order"]')).to_have_value("0,1,2,-")
    pw.expect(editor.locator('[name="mb_order"]')).to_have_value("0,1,2,3")
    page.context.close()


def test_failed_partial_confirmation_preserves_review_during_inbox_refresh(
    browser_factory, reset_demo_server
):
    base = reset_demo_server
    browser = browser_factory("chromium")
    page = browser.new_page()
    page.on("dialog", lambda dialog: dialog.accept())
    aid, review = _review(page, base)
    assert page.request.post(
        f"{base}/manual/{aid}/assign",
        headers={"HX-Request": "true"},
        form={"mbid": "demo-rel-folksmen"},
    ).ok
    page.reload()
    review = _opened(page, page.locator(f"#task-{aid}"))
    review.get_by_role("button", name="Edit track assignments").click()
    editor = review.locator(".assignment-editor")
    before = editor.locator('[name="disk_order"]').input_value()
    editor.get_by_role("button", name="Accept changes").click()
    accept = editor.get_by_role("button", name="Confirm suggestion", exact=True)
    pw.expect(accept).to_be_enabled()
    page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")
    fingerprint = editor.locator('[name="release_fingerprint"]').input_value()
    editor.locator('[name="release_fingerprint"]').evaluate(
        "e => e.value = e.value.split(':')[0] + ':obsolete'"
    )

    def poll_during_accept(route):
        assert parse_qs(route.request.post_data)["release_fingerprint"] == [
            fingerprint.split(":")[0] + ":obsolete"
        ]
        response = route.fetch()
        # Queue the normal Inbox refresh while acceptance is in flight.
        page.evaluate("htmx.trigger(document.body, 'tasks-changed')")
        pw.expect(editor.locator('[name="disk_order"]')).to_have_value(before)
        route.fulfill(response=response)

    page.route(f"**/confirm/{aid}/accept", poll_during_accept)
    with page.expect_response(lambda r: r.url.endswith("/tasks")):
        accept.click()
    pw.expect(editor.get_by_role("alert")).to_contain_text("Refresh the comparison")
    pw.expect(accept).to_be_enabled()
    pw.expect(accept).to_have_css("opacity", "1")
    pw.expect(editor.locator('[name="disk_order"]')).to_have_value(before)
    page.unroute(f"**/confirm/{aid}/accept", poll_during_accept)
    editor.locator('[name="release_fingerprint"]').evaluate(
        "(e, value) => e.value = value", fingerprint
    )
    with page.expect_response(lambda r: r.url.endswith(f"/confirm/{aid}/accept")) as applied:
        accept.click()
    assert "confirmation-applied" in applied.value.headers.get("hx-trigger", "")
    page.context.close()


def test_album_assignment_editor_cancels_inline_without_artwork(browser_factory, reset_demo_server):
    browser = browser_factory("chromium")
    page = browser.new_page()
    page.goto(f"{reset_demo_server}/album/demo-rel-dingoes")
    tracks = page.locator("#album-tracks")
    edit = tracks.get_by_role("button", name="Edit track assignments")
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    edit.click()
    editor = tracks.locator(".assignment-content")
    pw.expect(editor).to_be_visible()
    pw.expect(page.get_by_role("dialog")).not_to_be_visible()
    pw.expect(tracks.locator(".tracklist")).not_to_be_visible()
    original = editor.locator('[name="disk_order"]').input_value()
    editor.get_by_role("button", name="Move on-disk entry down", exact=True).first.click()
    pw.expect(editor.locator('[name="disk_order"]')).not_to_have_value(original)
    draft = editor.locator('[name="disk_order"]').input_value()
    # A late comparison must not replace an active draft.
    with page.expect_response(lambda r: "/compare" in r.url):
        page.evaluate("""htmx.ajax('GET', '/library/demo-rel-dingoes/compare',
                {target: '#compare-demo-rel-dingoes', swap: 'innerHTML'})""")
    pw.expect(editor.locator('[name="disk_order"]')).to_have_value(draft)
    editor.get_by_role("button", name="Reset", exact=True).click()
    pw.expect(editor.locator('[name="disk_order"]')).to_have_value(original)
    editor.get_by_role("button", name="Cancel", exact=True).click()
    pw.expect(tracks.locator(".tracklist")).to_be_visible()
    pw.expect(editor).to_have_count(0)
    pw.expect(edit).to_be_enabled()
    pw.expect(edit).to_have_css("opacity", "1")
    pw.expect(edit).to_be_focused()
    edit.click()
    pw.expect(editor.locator('[name="disk_order"]')).to_have_value(original)
    # Also cancel without a comparison replacing the original trigger.
    editor.get_by_role("button", name="Cancel", exact=True).click()
    pw.expect(edit).to_be_enabled()
    pw.expect(edit).to_have_css("opacity", "1")
    pw.expect(edit).to_be_focused()
    edit.click()
    pw.expect(editor.locator('[name="disk_order"]')).to_have_value(original)
    pw.expect(editor.get_by_role("checkbox", name="Use artwork")).to_have_count(0)
    assert not any("/assignments/" in url and "/artwork" in url for url in requests)
    editor.get_by_role("button", name="Move on-disk entry down", exact=True).first.click()
    with page.expect_response(
        lambda r: r.url.endswith("/confirm/demo-rel-dingoes/accept")
    ) as applied:
        editor.get_by_role("button", name="Accept changes").click()
    assert parse_qs(applied.value.request.post_data)["include_artwork"] == ["false"]
    pw.expect(edit).to_be_visible()
    pw.expect(tracks.locator(".tracklist")).to_be_visible()
    page.context.close()


def test_unassigned_files_are_proposed_and_remain_editable_until_acceptance(
    browser_factory, reset_demo_server
):
    """The real editor labels proposals and lets the user alter them before writing."""
    from bs4 import BeautifulSoup

    base = reset_demo_server
    browser = browser_factory("chromium")
    page = browser.new_page()
    aid, _ = _review(page, base)
    # Seed a release-only confirmation through the real guarded API. The
    # three files retain their numbers and receive no track identities.
    initial = page.request.get(f"{base}/confirm/{aid}/preview?release_only=true")
    fields = {
        item["name"]: item.get("value", "")
        for item in BeautifulSoup(initial.text(), "html.parser").select("input[type=hidden][name]")
    }
    accepted = page.request.post(
        f"{base}/confirm/{aid}", headers={"HX-Request": "true"}, form=fields
    )
    assert accepted.ok
    page.goto(f"{base}/album/{fields['candidate_mbid']}")
    editor = page.locator("#album-track-editor")
    pw.expect(editor.locator("[data-proposed-pair]")).to_have_count(3)
    pw.expect(editor.locator("[name=disk_order]")).to_have_value("0,1,2")
    editor.get_by_role("button", name="Move on-disk entry down", exact=True).first.click()
    pw.expect(editor.locator("[name=disk_order]")).to_have_value("1,0,2")
    pw.expect(editor.locator("[data-proposed-pair]")).to_have_count(3)
    # The finding takes us to the open review without resetting its draft.
    page.get_by_role("button", name="Review assignments", exact=True).click()
    pw.expect(editor).to_be_focused()
    pw.expect(editor.locator("[name=disk_order]")).to_have_value("1,0,2")
    # Refreshing the comparison preserves a draft already being edited.
    with page.expect_response(lambda r: "/compare" in r.url):
        page.evaluate(
            """id => htmx.ajax('GET', '/library/' + id + '/compare',
                {target: '#compare-' + id, swap: 'innerHTML'})""",
            fields["candidate_mbid"],
        )
    pw.expect(editor.locator("[name=disk_order]")).to_have_value("1,0,2")
    editor.get_by_role("button", name="Cancel", exact=True).click()
    page.get_by_role("button", name="Review assignments", exact=True).click()
    pw.expect(editor.locator("[name=disk_order]")).to_have_value("0,1,2")
    editor.get_by_role("button", name="Accept changes").click()
    pw.expect(page.locator("#album-tracks .tracklist")).to_be_visible()
    pw.expect(page.get_by_role("button", name="Review assignments", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Edit track assignments", exact=True).click()
    pw.expect(editor.locator("[data-proposed-pair]")).to_have_count(0)
    page.context.close()
