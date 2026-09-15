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
        host.get_by_role("button", name="Confirm release", exact=True).click()
        dialog = page.get_by_role("dialog")
        pw.expect(dialog).to_be_visible()
        candidate = dialog.get_by_alt_text("Artwork for selected release")
        pw.expect(candidate).to_be_visible()
        page.wait_for_function(
            "() => document.querySelector('dialog img[alt=\"Artwork for selected release\"]').naturalWidth > 0"
        )
        candidate_digest = candidate.get_attribute("src").rsplit("/", 1)[-1]
        before = dialog.locator('img[alt^="Current artwork"]').evaluate_all(
            "els => els.map(e => e.src.split('/').pop())"
        )
        checkbox = dialog.get_by_role("checkbox", name="Use artwork from the selected release")
        pw.expect(checkbox).to_be_checked()
        checkbox.set_checked(included)
        with page.expect_response(lambda r: r.url.endswith("/confirm/" + aid)) as confirmed:
            dialog.get_by_role(
                "button", name="Confirm release and apply changes", exact=True
            ).click()
        assert (
            parse_qs(confirmed.value.request.post_data)["include_artwork"][-1]
            == str(included).lower()
        )
        if on_album:
            page.wait_for_url(f"{base}/album/demo-rel-thamesmen")
        else:
            pw.expect(dialog).not_to_be_visible()
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


def test_changed_confirmation_stays_open_for_review(reset_demo_server):
    base = reset_demo_server
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        aid = _suggest(page, base)
        page.goto(base)
        page.locator(f"#task-{aid}").get_by_role(
            "button", name="Confirm release", exact=True
        ).click()
        dialog = page.get_by_role("dialog")
        pw.expect(dialog).to_be_visible()
        # A stale explicit pairing must be reviewed again before it can be saved.
        dialog.locator('[name="release_fingerprint"]').evaluate("e => e.value = 'obsolete'")
        dialog.get_by_role("button", name="Confirm release and apply changes", exact=True).click()
        pw.expect(dialog.get_by_role("alert")).to_contain_text("reset the assignments")
        pw.expect(dialog).to_be_visible()
        pw.expect(
            dialog.get_by_role("button", name="Confirm release and apply changes", exact=True)
        ).to_be_disabled()
        dialog.get_by_role("button", name="Close confirmation", exact=True).click()
        card = page.locator(f"#task-{aid}")
        card.get_by_role("button", name="Edit track assignments").click()
        card.get_by_role("button", name="Reset", exact=True).click()
        card.get_by_role("button", name="Accept changes", exact=True).click()
        pw.expect(
            dialog.get_by_role("button", name="Confirm release and apply changes")
        ).to_be_enabled()
        browser.close()
