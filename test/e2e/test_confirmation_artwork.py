"""Release confirmation scopes artwork from both of its hosts (#483)."""

from __future__ import annotations

import os
from urllib.parse import parse_qs

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")


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
        if included:
            review.get_by_role("button", name="Confirm release", exact=True).click()
            dialog = page.locator("#confirmation-modal dialog")
            pw.expect(dialog.get_by_role("region", name="Artwork replacement")).to_be_visible()
            with page.expect_response(lambda r: r.url.endswith("/confirm/" + aid)) as confirmed:
                dialog.get_by_role("button", name="Confirm release", exact=True).click()
        else:
            with page.expect_response(
                lambda r: r.url.endswith(f"/confirm/{aid}/accept")
            ) as confirmed:
                review.get_by_role("button", name="Confirm release", exact=True).click()
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
            review.get_by_role("button", name="Confirm release", exact=True).click()
        assert pending
        assert parse_qs(applied.value.request.post_data)["include_artwork"] == ["false"]
        assert "confirmation-applied" in applied.value.headers.get("hx-trigger", "")
        for route in pending:
            route.abort()
        browser.close()


def test_changed_confirmation_stays_open_for_review(reset_demo_server):
    base = reset_demo_server
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        aid = _suggest(page, base)
        page.goto(base)
        card = page.locator(f"#task-{aid}")
        card.get_by_role("button", name="Edit track assignments").click()
        editor = card.locator(".assignment-editor")
        pw.expect(editor.get_by_role("button", name="Accept changes")).to_be_visible()
        # A stale explicit pairing must be reviewed again before it can be saved.
        editor.locator('[name="release_fingerprint"]').evaluate(
            "e => e.value = e.value.split(':')[0] + ':obsolete'"
        )
        editor.get_by_role("button", name="Accept changes").click()
        dialog = page.locator("#confirmation-modal dialog")
        pw.expect(dialog.get_by_role("alert")).to_contain_text("Refresh the comparison")
        pw.expect(dialog).to_be_visible()
        pw.expect(dialog.get_by_role("button", name="Confirm release", exact=True)).to_have_count(0)
        dialog.get_by_role("button", name="Close confirmation", exact=True).click()
        card.get_by_role("button", name="Reset", exact=True).click()
        pw.expect(editor.locator('[name="release_fingerprint"]')).not_to_have_value("obsolete")
        with page.expect_response(lambda r: r.url.endswith(f"/confirm/{aid}/accept")) as applied:
            card.get_by_role("button", name="Accept changes", exact=True).click()
        assert "confirmation-applied" in applied.value.headers.get("hx-trigger", "")
        browser.close()
