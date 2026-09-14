"""Artwork controls remain usable across overlapping and settling swaps (#477)."""

from __future__ import annotations

import os
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
    page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")
    load = page.get_by_role("button", name="Load the Cover Art Archive's cover")
    if load.count():
        load.click()
        page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")
    return aid


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
                    !e.detail.requestConfig.path.endsWith('?use=archive')) return;
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
            page.get_by_role("button", name="Use the Cover Art Archive's artwork").click()
        assert applied.value.request.method == "POST"
        assert applied.value.request.headers.get("hx-request") == "true"
        assert parse_qs(applied.value.request.post_data)["use"] == ["archive"]
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
        page.get_by_role("button", name="Use the Cover Art Archive's artwork").click()
        chosen = page.get_by_role("button", name="Go back to the best image")
        pw.expect(chosen).to_be_visible()
        # The checked date and artwork finding ride on this response out of
        # band; rejecting only its main fragment would still corrupt the scope.
        route, response = held.pop()
        with page.expect_response(lambda r: r.url.endswith("?reread=1")):
            route.fulfill(response=response)
        page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")
        pw.expect(chosen).to_be_visible()
        assert page.locator(".apply-art-" + aid + '[name="use"]').input_value() == "archive"
        page.on("dialog", lambda dialog: dialog.accept())
        with page.expect_response(lambda r: r.url.endswith("/artwork/update")) as applied:
            page.get_by_role("button", name="Apply this album's artwork").click()
        assert parse_qs(applied.value.request.post_data)["use"] == ["archive"]
        browser.close()
