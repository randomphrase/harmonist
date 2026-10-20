"""A slow artwork apply owns both controls until the album reflects its result (#786)."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize("primary", [True, False])
def test_slow_apply_blocks_competing_requests_and_refreshes_album(reset_demo_server, primary):
    aid = "demo-rel-otis-day"
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280 if primary else 390, "height": 900})
        prepared = page.request.post(
            f"{reset_demo_server}/retag/{aid}",
            headers={"HX-Request": "true"},
            form={"include_artwork": "false"},
        )
        assert prepared.ok
        page.goto(f"{reset_demo_server}/album/{aid}")
        page.wait_for_selector("#album-artwork .art-row")
        page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")
        cover = page.locator(f'#album-identity img[src="/cover/{aid}"]')
        pw.expect(cover).to_be_hidden()
        page.locator("#album-artwork button.art-pick__use:visible").click()
        page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")
        top = page.locator(".album-artwork-apply button")
        bottom = page.get_by_role("button", name="Apply this album's artwork")
        pw.expect(top).to_be_visible()
        pw.expect(bottom).to_be_visible()
        unavailable = page.locator("#album-artwork button:disabled").count()
        # A failed transport must release the shared busy state for a retry.
        page.route(
            "**/artwork/update",
            lambda route: route.fulfill(status=503, body="Temporarily unavailable"),
            times=1,
        )
        with page.expect_response(lambda response: response.url.endswith("/artwork/update")):
            (top if primary else bottom).click()
        pw.expect(top).to_be_enabled()
        pw.expect(bottom).to_be_enabled()
        pw.expect(top).to_have_text("Apply updates", use_inner_text=True, ignore_case=True)
        pw.expect(bottom).to_have_text("Apply artwork", use_inner_text=True, ignore_case=True)
        assert page.locator("#album-artwork button:disabled").count() == unavailable
        # Hold the request before it reaches the server, and forward it only on
        # release. Fetching inside the handler appended only once the real apply
        # returned, so the count below raced the server's write (#824).
        held = []
        page.route("**/artwork/update", lambda route: held.append(route))
        requests = []
        page.on("request", lambda request: requests.append(request.url))
        (top if primary else bottom).click()
        pw.expect(page.locator(f"#tagging-{aid}")).to_be_visible()
        pw.expect(page.locator(f"#tagging-{aid}")).to_contain_text("Applying artwork…")
        pw.expect(page.locator("button:visible:enabled")).to_have_count(0)
        pw.expect(top).to_be_disabled()
        pw.expect(bottom).to_be_disabled()
        pw.expect(top).to_have_text("Applying artwork…", use_inner_text=True, ignore_case=True)
        pw.expect(bottom).to_have_text("Applying artwork…", use_inner_text=True, ignore_case=True)
        # Native clicks and programmatic submissions must not send a second
        # write, and a refresh must not become the latest artwork request.
        page.evaluate(
            """id => {
                document.querySelectorAll('form[hx-post$="/artwork/update"]')
                    .forEach(form => form.requestSubmit());
                htmx.ajax('GET', '/album/' + id + '/artwork', '#album-artwork');
            }""",
            aid,
        )
        page.wait_for_timeout(100)
        assert len([url for url in requests if "/artwork" in url]) == 1
        assert len(held) == 1
        route = held.pop()
        with page.expect_navigation():
            route.fulfill(response=route.fetch())
        page.wait_for_selector("#album-artwork .art-row")
        page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")
        pw.expect(cover).to_be_visible()
        assert cover.evaluate("image => image.complete && image.naturalWidth > 0")
        pw.expect(page.locator(".album-history")).to_contain_text("Updated artwork on 2 files")
        pw.expect(page.locator("#album-artwork")).not_to_contain_text(
            "changed after the page showed it"
        )
        pw.expect(page.locator('form[hx-post$="/artwork/update"]')).to_have_count(0)
        browser.close()
