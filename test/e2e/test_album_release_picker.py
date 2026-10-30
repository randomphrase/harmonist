"""The album remains the place to pick, review, cancel and confirm a release."""

import os

import pytest

from harmonist import formats, sidecar

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw_api = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize("on_album,width", [(True, 1280), (False, 390)])
def test_forget_then_choose_another_release(
    browser_factory, release_picker_server, htmx_ready, tmp_path, on_album, width
):
    page = browser_factory("chromium").new_page(viewport={"width": width, "height": 900})
    base = release_picker_server
    folder = (
        tmp_path
        / "harmonist-demo"
        / "Dingoes Ate My Baby"
        / "Little Bit o' Hoot, Whole Lotta Nanny"
    )
    originals = {p: p.read_bytes() for p in folder.glob("*.m4a")}
    assert len(originals) == 3
    dialogs = []

    def capture_dialog(dialog):
        dialogs.append(dialog.message)
        dialog.accept()

    page.on("dialog", capture_dialog)
    page.goto(f"{base}/album/demo-rel-dingoes")
    with page.expect_response(lambda r: r.url.endswith("/forget/demo-rel-dingoes") and r.ok):
        htmx_ready(page.get_by_role("button", name="Forget", exact=True)).click()
    page.wait_for_url(f"{base}/")
    assert dialogs == []
    card = page.locator('div[id^="task-"].relative').filter(has_text="Little Bit o' Hoot")
    pw_api.expect(card).to_have_count(1)
    aid = card.get_attribute("id").removeprefix("task-")
    assert sidecar.read(folder) is None
    assert {p: p.read_bytes() for p in originals} == originals
    if on_album:
        page.goto(f"{base}/album/{aid}")
    panel = page.locator("#album-inbox-actions") if on_album else card
    htmx_ready(panel.get_by_role("radio", name="By name", exact=True)).check()
    panel.get_by_placeholder("Album", exact=True).fill("Another release")
    with page.expect_response(lambda r: r.url.endswith(f"/manual/{aid}/search") and r.ok):
        htmx_ready(panel.get_by_role("button", name="Search", exact=True)).click()
    choices = panel.get_by_role("list", name="Release candidates")
    pw_api.expect(choices).to_contain_text("Another release")
    with page.expect_response(lambda r: r.url.endswith(f"/manual/{aid}/assign") and r.ok):
        htmx_ready(choices.get_by_role("button", name="Use", exact=True)).click()
    confirm = panel.get_by_role("button", name="Confirm suggestion", exact=True)
    pw_api.expect(confirm).to_be_visible()
    pw_api.expect(panel).to_contain_text("Another release")
    assert sidecar.read(folder).mb_match_candidate.mb_release_id == "demo-rel-picker-other"
    assert {p: p.read_bytes() for p in originals} == originals
    with page.expect_response(lambda r: r.url.endswith(f"/confirm/{aid}/accept") and r.ok):
        htmx_ready(confirm).click()
    if on_album:
        page.wait_for_url(f"{base}/album/demo-rel-picker-other")
        pw_api.expect(page.locator("#album-identity")).to_contain_text("Another release")
    else:
        pw_api.expect(card).to_have_count(0)
    assert sidecar.read(folder).mb_release_id == "demo-rel-picker-other"
    assert all(formats.read_album_id(p) == "demo-rel-picker-other" for p in originals)
    assert dialogs == []
    page.context.close()


def _open_picker(page, server, ready):
    page.goto(f"{server}/album/demo-rel-dingoes?from_q=Dingoes")
    dialogs = []

    def dismiss(d):
        dialogs.append(d.message)
        d.dismiss()

    page.on("dialog", dismiss)
    with page.expect_response(lambda r: r.url.endswith("/rematch")) as response:
        ready(
            page.get_by_role("button", name="Wrong MusicBrainz match — pick the correct release")
        ).click()
    assert response.value.ok
    panel = page.locator("#album-inbox-actions")
    pw_api.expect(
        panel.get_by_role("region", name="Releases in this release group")
    ).to_be_visible()
    assert "/album/" in page.url and "from_q=Dingoes" in page.url
    assert dialogs == []
    return panel, dialogs, dismiss


@pytest.mark.parametrize("width", [1280, 390])
def test_sibling_cancel_and_restore_keep_album_context(
    browser_factory, release_picker_server, htmx_ready, width
):
    browser = browser_factory("chromium")
    page = browser.new_page(viewport={"width": width, "height": 900})
    panel, dialogs, _ = _open_picker(page, release_picker_server, htmx_ready)
    choices = panel.get_by_role("region", name="Releases in this release group")
    row = choices.get_by_role("listitem").filter(has_text="Alternate pressing")
    htmx_ready(row.get_by_role("button", name="Use", exact=True)).press("Enter")
    pw_api.expect(panel.get_by_role("button", name="Confirm suggestion")).to_be_visible()
    pw_api.expect(panel).to_contain_text("Alternate pressing")
    htmx_ready(panel.get_by_role("button", name="Cancel", exact=True)).click()
    pw_api.expect(choices).to_be_visible()
    page.reload()
    pw_api.expect(choices).to_contain_text("Alternate pressing")
    htmx_ready(panel.get_by_role("button", name="Revert to Previous")).click()
    page.wait_for_url("**/album/demo-rel-dingoes?from_q=Dingoes")
    pw_api.expect(page.locator("#album-identity")).to_contain_text("MB release")
    assert dialogs == []
    page.context.close()


@pytest.mark.parametrize("method", ["search", "paste"])
def test_reviewed_incomplete_release_applies_with_one_confirmation(
    browser_factory, release_picker_server, htmx_ready, method
):
    browser = browser_factory("chromium")
    page = browser.new_page()
    panel, dialogs, _ = _open_picker(page, release_picker_server, htmx_ready)
    if method == "search":
        panel.get_by_role("radio", name="By name", exact=True).check()
        panel.get_by_placeholder("Album", exact=True).fill("Another release")
        htmx_ready(panel.get_by_role("button", name="Search", exact=True)).click()
        results = panel.locator('[id^="mbid-results-"]')
        htmx_ready(results.get_by_role("button", name="Use", exact=True)).click()
    else:
        panel.get_by_role("radio", name="By MBID", exact=True).check()
        paste = panel.locator('form[hx-post$="/assign"]').filter(
            has=page.locator('input[name="mbid"][type="text"]')
        )
        paste.get_by_placeholder("MB release URL or 36-char MBID").fill("demo-rel-picker-other")
        htmx_ready(paste.get_by_role("button", name="Use", exact=True)).click()
    confirm = panel.get_by_role("button", name="Confirm suggestion")
    pw_api.expect(confirm).to_be_visible()
    pw_api.expect(panel).to_contain_text("Another release")
    pw_api.expect(panel).to_contain_text("On disk · 3 files")
    pw_api.expect(panel).to_contain_text("4 tracks")
    htmx_ready(confirm).click()
    assert dialogs == []
    page.wait_for_url("**/album/demo-rel-picker-other?from_q=Dingoes")
    pw_api.expect(page.locator("#album-identity")).to_contain_text("3 of 4")
    page.context.close()


@pytest.mark.parametrize("on_album,width", [(True, 1280), (False, 390)])
def test_missing_disc_numbers_confirm_review_once(
    browser_factory, release_picker_server, htmx_ready, tmp_path, on_album, width
):
    browser = browser_factory("chromium")
    page = browser.new_page(viewport={"width": width, "height": 900})
    panel, dialogs, _ = _open_picker(page, release_picker_server, htmx_ready)
    aid = page.url.split("/album/")[1].split("?")[0]
    files = [
        p
        for p in (tmp_path / "harmonist-demo").rglob("*.m4a")
        if formats.read_album_id(p) == "demo-rel-dingoes"
    ]
    assert len(files) == 3
    for path in files:
        formats.write_owned(path, dict(formats.read_owned(path), disc_num=None))
    before = [p.read_bytes() for p in files]
    if not on_album:
        page.goto(release_picker_server)
        panel = page.locator(f"#task-{aid}")
    choices = panel.get_by_role("region", name="Releases in this release group")
    row = choices.get_by_role("listitem").filter(has_text="Alternate pressing")
    htmx_ready(row.get_by_role("button", name="Use", exact=True)).click()
    review = panel.locator(".assignment-editor")
    pw_api.expect(review.locator("[data-assignment-row]")).to_have_count(3)
    pw_api.expect(review.locator('[aria-label^="Track numbering changes"]')).to_have_count(0)
    assert [p.read_bytes() for p in files] == before
    htmx_ready(review.get_by_role("button", name="Confirm suggestion")).click()
    assert dialogs == []
    if on_album:
        page.wait_for_url("**/album/demo-rel-picker-sibling?from_q=Dingoes")
    else:
        pw_api.expect(panel).to_have_count(0)
    assert all(formats.read_album_id(p) == "demo-rel-picker-sibling" for p in files)
    assert all(formats.read_tags(p).disc_num == 1 for p in files)
    page.context.close()


@pytest.mark.parametrize("on_album", [True, False])
def test_release_modes_show_one_result_list_even_after_a_late_search(
    browser_factory, release_picker_server, htmx_ready, on_album
):
    browser = browser_factory("chromium")
    page = browser.new_page()
    browses = []
    page.on("request", lambda r: browses.append(r.url) if "/siblings?" in r.url else None)
    panel, _, _ = _open_picker(page, release_picker_server, htmx_ready)
    if not on_album:
        aid = page.url.split("/album/")[1].split("?")[0]
        page.goto(release_picker_server)
        panel = page.locator(f"#task-{aid}")
    siblings = panel.get_by_role("radio", name="In this release group", exact=True)
    choices = panel.get_by_role("region", name="Releases in this release group")
    pw_api.expect(siblings).to_be_checked()
    pw_api.expect(choices).to_contain_text("Alternate pressing")
    revert = panel.get_by_role("button", name="Revert to Previous")
    pw_api.expect(revert).to_be_visible()
    assert panel.evaluate("""panel =>
            panel.querySelector('button[hx-post$="/rematch/undo"]').getBoundingClientRect().top >
            panel.querySelector('[aria-label="Releases in this release group"]').getBoundingClientRect().bottom
        """)
    pw_api.expect(panel.get_by_role("radio", name="Store URL", exact=True)).to_have_count(0)
    before = len(browses)
    pending = []
    page.route("**/manual/*/search", lambda route: pending.append(route))
    panel.get_by_role("radio", name="By name", exact=True).check()
    pw_api.expect(choices).not_to_be_visible()
    pw_api.expect(revert).to_be_visible()
    panel.get_by_placeholder("Album", exact=True).fill("Another release")
    with page.expect_request(lambda r: "/manual/" in r.url and r.url.endswith("/search")):
        htmx_ready(panel.get_by_role("button", name="Search", exact=True)).click()
    siblings.focus()
    siblings.press("Space")
    pw_api.expect(choices).to_be_visible()
    assert pending
    with page.expect_response(lambda r: "/manual/" in r.url and r.url.endswith("/search")):
        pending.pop().continue_()
    results = panel.locator('[id^="mbid-results-"]')
    pw_api.expect(results).to_contain_text("Another release")
    pw_api.expect(results).not_to_be_visible()
    assert len(browses) == before
    panel.get_by_role("radio", name="By MBID", exact=True).check()
    pw_api.expect(choices).not_to_be_visible()
    paste = panel.get_by_placeholder("MB release URL or 36-char MBID")
    pw_api.expect(paste).to_be_visible()
    pw_api.expect(results).not_to_be_visible()
    pw_api.expect(revert).to_be_visible()
    assert panel.evaluate("""panel =>
            panel.querySelector('button[hx-post$="/rematch/undo"]').getBoundingClientRect().top >
            panel.querySelector('input[name="mbid"][type="text"]').getBoundingClientRect().bottom
        """)
    page.unroute("**/manual/*/search")
    panel.get_by_role("radio", name="By name", exact=True).check()
    pw_api.expect(paste).not_to_be_visible()
    htmx_ready(panel.get_by_role("button", name="Search", exact=True)).click()
    use = results.get_by_role("button", name="Use", exact=True)
    pw_api.expect(use).to_be_visible()
    pw_api.expect(panel.get_by_role("button", name="Confirm suggestion")).to_have_count(0)
    htmx_ready(use).click()
    pw_api.expect(panel.get_by_role("button", name="Confirm suggestion")).to_be_visible()
    htmx_ready(panel.get_by_role("button", name="Cancel", exact=True)).click()
    pw_api.expect(siblings).to_be_visible()
    siblings.check()
    pw_api.expect(choices).to_be_visible()
    pw_api.expect(results).not_to_be_visible()
    page.context.close()


@pytest.mark.parametrize("on_album", [True, False])
def test_no_siblings_automatically_searches_by_name(
    browser_factory, release_picker_no_siblings_server, htmx_ready, on_album
):
    browser = browser_factory("chromium")
    page = browser.new_page()
    server = release_picker_no_siblings_server
    page.goto(f"{server}/album/demo-rel-dingoes")
    htmx_ready(
        page.get_by_role("button", name="Wrong MusicBrainz match — pick the correct release")
    ).click()
    page.wait_for_url(lambda url: "demo-rel-dingoes" not in url)
    panel = page.locator("#album-inbox-actions")
    pw_api.expect(panel.get_by_role("radio", name="By name", exact=True)).to_be_checked()
    if not on_album:
        aid = page.url.split("/album/")[1]
        page.goto(server)
        panel = page.locator(f"#task-{aid}")
    pw_api.expect(panel.get_by_role("radio", name="By name", exact=True)).to_be_checked()
    results = panel.locator('[id^="mbid-results-"]')
    pw_api.expect(results.get_by_role("button", name="Use", exact=True)).to_be_visible()
    pw_api.expect(panel.get_by_role("button", name="Revert to Previous")).to_be_visible()
    pw_api.expect(
        panel.get_by_role("region", name="Releases in this release group")
    ).not_to_be_visible()
    page.context.close()


def test_late_empty_group_preserves_the_chosen_mode(
    browser_factory, release_picker_no_siblings_server, htmx_ready
):
    browser = browser_factory("chromium")
    page = browser.new_page()
    pending = []
    searches = []
    page.route("**/manual/*/siblings?*", lambda route: pending.append(route))
    page.on("request", lambda r: searches.append(r.url) if r.url.endswith("/search") else None)
    page.goto(f"{release_picker_no_siblings_server}/album/demo-rel-dingoes")
    with page.expect_request(lambda r: "/siblings?" in r.url):
        htmx_ready(
            page.get_by_role("button", name="Wrong MusicBrainz match — pick the correct release")
        ).click()
    panel = page.locator("#album-inbox-actions")
    panel.get_by_role("radio", name="By MBID", exact=True).check()
    paste = panel.get_by_placeholder("MB release URL or 36-char MBID")
    paste.fill("demo-rel-picker-other")
    with page.expect_response(lambda r: "/siblings?" in r.url):
        pending.pop().continue_()
    pw_api.expect(panel.get_by_role("button", name="Revert to Previous")).to_be_visible()
    pw_api.expect(panel.get_by_role("radio", name="By MBID", exact=True)).to_be_checked()
    pw_api.expect(paste).to_be_visible()
    pw_api.expect(paste).to_have_value("demo-rel-picker-other")
    assert searches == []
    page.context.close()


@pytest.mark.parametrize("on_album,width", [(True, 1280), (False, 390)])
def test_missing_file_id_recovers_to_working_search(
    browser_factory, release_picker_server, htmx_ready, tmp_path, on_album, width
):
    browser = browser_factory("chromium")
    page = browser.new_page(viewport={"width": width, "height": 900})
    _open_picker(page, release_picker_server, htmx_ready)
    aid = page.url.split("/album/")[1].split("?")[0]
    track = next(
        p
        for p in (tmp_path / "harmonist-demo").rglob("*.m4a")
        if formats.read_album_id(p) == "demo-rel-dingoes"
    )
    formats.write_owned(track, dict(formats.read_owned(track), mb_album_id=None))
    before = track.read_bytes()
    if on_album:
        page.reload()
        panel = page.locator("#album-inbox-actions")
    else:
        page.goto(release_picker_server)
        panel = page.locator(f"#task-{aid}")
    pw_api.expect(panel.get_by_role("radio", name="By name", exact=True)).to_be_checked()
    results = panel.locator('[id^="mbid-results-"]')
    use = results.get_by_role("button", name="Use", exact=True).first
    pw_api.expect(use).to_be_visible()
    pw_api.expect(panel.get_by_role("button", name="Revert to Previous")).to_have_count(0)
    panel.get_by_role("radio", name="In this release group", exact=True).check()
    pw_api.expect(panel.get_by_role("alert")).to_contain_text("same MusicBrainz release")
    pw_api.expect(panel.get_by_role("button", name="Retry", exact=True)).to_have_count(0)
    panel.get_by_role("radio", name="By name", exact=True).check()
    htmx_ready(panel.get_by_role("button", name="Search", exact=True)).click()
    htmx_ready(use).click()
    pw_api.expect(panel.get_by_role("button", name="Confirm suggestion")).to_be_visible()
    assert track.read_bytes() == before
    page.context.close()
