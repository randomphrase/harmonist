"""Release confirmation scopes artwork from both of its hosts (#483)."""

from __future__ import annotations

import os
from urllib.parse import parse_qs

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")


def test_review_outcome_matches_the_images_written(confirmation_outcome_server):
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
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.goto(f"{base}/album/demo-rel-dingoes")
        page.locator("#contribution-editions-demo-rel-dingoes tbody tr").filter(
            has_text="Suggested"
        ).get_by_role("button", name="Use", exact=True).click()
        review = page.get_by_role("region", name="Review suggested release")
        artwork = review.locator(".assignment-artwork")
        keep = artwork.get_by_text("Keep existing artwork", exact=True)
        pw.expect(keep).to_be_visible()
        candidate = artwork.get_by_alt_text("Artwork for selected release", exact=True)
        if scenario in {"identical", "tracks-only"}:
            pw.expect(candidate).to_have_count(0)
            pw.expect(artwork).to_contain_text("Selected-release image already present")
        else:
            pw.expect(candidate).to_be_visible()
            pw.expect(artwork).not_to_contain_text("Selected-release image already present")
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
            pw.expect(keep).to_be_hidden()
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
            pw.expect(keep).to_be_visible(visible=not included)
        else:
            pw.expect(checkbox).to_have_count(0)
        if scenario.startswith("protected"):
            pw.expect(artwork).to_contain_text("Differing per-track artwork is preserved")
        confirm = review.get_by_role("button", name="Confirm suggestion", exact=True)
        pw.expect(confirm).to_have_count(1)
        with page.expect_response(
            lambda r: "/confirm/" in r.url and "/accept" in r.url
        ) as response:
            confirm.click()
        assert "confirmation-applied" in response.value.headers.get("hx-trigger", "")
        page.wait_for_url("**/album/demo-rel-dingoes-digital*")
        for path in paths:
            assert formats.read_album_id(path) == "demo-rel-dingoes-digital"
            assert formats.read_cover(path) == (
                (selected, "image/png")
                if included and scenario != "protected-folder"
                else before[path]
            )
        assert (cover.read_bytes() if cover.exists() else None) == (
            selected if included else old_cover
        )
        browser.close()


def _suggest(page, base):
    headers = {"HX-Request": "true"}
    assert page.request.post(f"{base}/library/demo-rel-dingoes/rematch", headers=headers).ok
    page.goto(base)
    card = page.locator('div[id^="task-"].relative').filter(
        has_text="Little Bit o' Hoot, Whole Lotta Nanny"
    )
    pw.expect(card).to_have_count(1)
    aid = card.get_attribute("id").removeprefix("task-")
    assert page.request.post(
        f"{base}/manual/{aid}/assign", headers=headers, form={"mbid": "demo-rel-thamesmen"}
    ).ok
    return aid


@pytest.mark.parametrize("on_album", [False, True])
def test_review_artwork_inspection_and_refresh_preserve_draft(reset_demo_server, on_album):
    base = reset_demo_server
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        aid = _suggest(page, base)
        page.goto(f"{base}/album/{aid}" if on_album else base)
        # The editor's host differs between the Inbox and album page.
        review = (page.locator("main") if on_album else page.locator(f"#task-{aid}")).locator(
            ".assignment-editor"
        )
        artwork = review.locator(".assignment-artwork")
        pw.expect(artwork.get_by_alt_text("Artwork for selected release")).to_be_visible()
        thumbs = artwork.locator("button[popovertarget]")
        assert thumbs.count() >= 2, "current and selected artwork must both open full size"
        sizes = []
        for thumb in thumbs.all():
            target = thumb.get_attribute("popovertarget")
            full = page.locator(f'[id="{target}"]')
            pw.expect(full).to_have_count(1)
            pw.expect(full.locator("img")).to_have_attribute(
                "src", thumb.locator("img").get_attribute("src")
            )
            sizes.append(thumb.evaluate("e => [e.offsetWidth, e.offsetHeight]"))
            pw.expect(thumb.locator("img")).to_have_css("object-fit", "contain")
            thumb.click()
            pw.expect(full).to_be_visible()
            assert full.evaluate("e => e.matches(':popover-open')")
            page.keyboard.press("Escape")
            pw.expect(full).not_to_be_visible()
        assert all(size == sizes[0] for size in sizes)
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
        assert parse_qs(refreshed.value.url.split("?", 1)[1])["release_fingerprint"] == [
            fingerprint
        ]
        pw.expect(timestamp).to_have_text("just now")
        pw.expect(artwork).to_contain_text("CAA checked")
        pw.expect(checkbox).to_be_checked()
        pw.expect(review.locator('[name="disk_order"]')).to_have_value("1,0,2")
        pw.expect(review.get_by_role("button", name="Accept changes", exact=True)).to_be_visible()
        browser.close()


@pytest.mark.parametrize("on_album", [False, True])
@pytest.mark.parametrize("included", [False, True])
def test_confirmation_applies_only_included_artwork(reset_demo_server, on_album, included):
    base = reset_demo_server
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
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
        review.get_by_role("button", name="Cancel", exact=True).click()
        pw.expect(checkbox).to_be_checked(checked=included)
        with page.expect_response(lambda r: r.url.endswith(f"/confirm/{aid}/accept")) as confirmed:
            review.get_by_role("button", name="Confirm suggestion", exact=True).click()
        assert "confirmation-applied" in confirmed.value.headers.get("hx-trigger", "")
        assert (
            parse_qs(confirmed.value.request.post_data)["include_artwork"][-1]
            == str(included).lower()
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
            pw.expect(
                page.get_by_role("button", name="Use the Cover Art Archive's artwork")
            ).to_be_visible()
        browser.close()


def test_pending_artwork_does_not_block_tags_only_acceptance(reset_demo_server):
    base = reset_demo_server
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        aid = _suggest(page, base)
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
        browser.close()


def test_changed_confirmation_preserves_review_and_artwork_choice(reset_demo_server):
    base = reset_demo_server
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
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
            card.get_by_role("button", name="Confirm suggestion", exact=True).click()
        assert "confirmation-applied" in applied.value.headers.get("hx-trigger", "")
        browser.close()
