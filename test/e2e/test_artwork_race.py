"""Artwork controls remain usable across overlapping and settling swaps (#477)."""

from __future__ import annotations

import os
import re
from urllib.parse import parse_qs

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")


def _ready(page, base):
    aid = "demo-rel-dingoes"
    page.request.post(
        f"{base}/retag/{aid}",
        headers={"HX-Request": "true"},
        form={"include_artwork": "false"},
    )
    page.goto(f"{base}/album/{aid}")
    page.wait_for_selector("#album-artwork .art-row")
    # The archive's listing arrives with the page's own check (#659).
    page.wait_for_selector("#album-artwork .art-pick__picker")
    page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")
    return aid


# A choice names the archive image by its id (#659).
CHOSEN = re.compile(r"=[0-9]+")


@pytest.mark.parametrize("primary", [False, True])
def test_apply_immediately_after_choosing_is_an_htmx_post(reset_demo_server, primary):
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        aid = _ready(page, reset_demo_server)
        before = page.locator(
            "#album-artwork .art-row__side:not(.art-row__side--after) img"
        ).evaluate_all("els => els.map(e => e.src)")
        page.on("dialog", lambda dialog: dialog.accept())
        # Widen the engine's normal 20ms settling window. Schedule an actual
        # button click on the next browser turn after insertion, before delayed
        # HTMX processing. No race against Playwright's actionability polling.
        page.evaluate(
            """primary => {
            htmx.config.defaultSettleDelay = 500;
            document.body.addEventListener('htmx:afterSwap', function choose(e) {
                if (e.target.id !== 'album-artwork' ||
                    !e.detail.xhr.responseURL.includes('pick=1')) return;
                document.body.removeEventListener('htmx:afterSwap', choose);
                setTimeout(() => {
                    const selector = primary ? '.album-artwork-apply button' :
                        '#album-artwork form[hx-post$="/artwork/update"] button';
                    document.querySelector(selector).click();
                }, 0);
            });
        }""",
            primary,
        )
        with page.expect_response(
            lambda r: r.url.endswith("/artwork/update"), timeout=5000
        ) as applied:
            page.locator("#album-artwork button.art-pick__use:visible").click()
        assert applied.value.request.method == "POST"
        assert applied.value.request.headers.get("hx-request") == "true"
        assert CHOSEN.search(parse_qs(applied.value.request.post_data)["use"][0])
        page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")
        assert page.url == f"{reset_demo_server}/album/{aid}"
        after = page.locator(
            "#album-artwork .art-row__side:not(.art-row__side--after) img"
        ).evaluate_all("els => els.map(e => e.src)")
        assert after != before
        browser.close()


def test_late_archive_check_cannot_erase_a_newer_choice(reset_demo_server):
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        aid = _ready(page, reset_demo_server)
        held = []

        def hold(route):
            # Run the real check and hold its response at the network boundary:
            # the stale ordinary preview arrives only when this test releases it.
            held.append((route, route.fetch()))
            page.evaluate("window.archiveResponseHeld = true")

        page.route("**/artwork?reread=1", hold)
        page.locator('[hx-target="#album-artwork"][hx-get$="?reread=1"]').click()
        page.wait_for_function("window.archiveResponseHeld === true")
        page.locator("#album-artwork button.art-pick__use:visible").click()
        # The choice is what the section carries: the field the combined
        # action and the Apply button send back.
        chosen = page.locator(".apply-art-" + aid + '[name="use"]')
        pw.expect(chosen).to_have_value(CHOSEN)
        # The checked date and artwork finding ride on this response out of
        # band; rejecting only its main fragment would still corrupt the scope.
        route, response = held.pop()
        with page.expect_response(lambda r: r.url.endswith("?reread=1")):
            route.fulfill(response=response)
        page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")
        # The stale ordinary preview did not replace the newer choice.
        pw.expect(chosen).to_have_value(CHOSEN)
        page.on("dialog", lambda dialog: dialog.accept())
        with page.expect_response(lambda r: r.url.endswith("/artwork/update")) as applied:
            page.get_by_role("button", name="Apply this album's artwork").click()
        assert CHOSEN.search(parse_qs(applied.value.request.post_data)["use"][0])
        browser.close()
