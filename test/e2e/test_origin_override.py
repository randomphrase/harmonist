"""Origin editing must send its request and refresh the album's conclusion."""

import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
playwright_sync = pytest.importorskip("playwright.sync_api")

ALBUM = "demo-rel-rural-juror"


@pytest.mark.parametrize(("engine", "width"), [("chromium", 1280), ("webkit", 390)])
def test_origin_save_cancel_retry_and_reset(reset_demo_server, htmx_ready, engine, width):
    with playwright_sync.sync_playwright() as pw:
        browser = getattr(pw, engine).launch()
        page = browser.new_page(viewport={"width": width, "height": 900})
        page.goto(f"{reset_demo_server}/album/{ALBUM}")
        origin = page.locator("dd[data-field='origin']")
        initial = origin.inner_text().strip()
        edit = page.get_by_role("button", name="Change origin", exact=True)
        dialog = page.get_by_role("dialog", name="Change origin")
        htmx_ready(edit).click()
        select = dialog.get_by_label("Origin", exact=True)
        playwright_sync.expect(select).to_be_focused()
        select.select_option("CD")
        # Cancel must neither submit nor leave the draft selected on reopening.
        dialog.get_by_role("button", name="Cancel", exact=True).click()
        playwright_sync.expect(dialog).to_have_count(0)
        htmx_ready(edit).click()
        playwright_sync.expect(select).to_have_value("automatic")
        select.select_option("CD")
        endpoint = f"**/library/{ALBUM}/origin"
        page.route(endpoint, lambda route: route.fulfill(status=500, body="Save failed"))
        with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/origin")
        ):
            htmx_ready(dialog.get_by_role("button", name="Save", exact=True)).click()
        playwright_sync.expect(dialog).to_be_visible()
        playwright_sync.expect(select).to_have_value("CD")
        playwright_sync.expect(
            dialog.get_by_role("button", name="Save", exact=True)
        ).to_be_enabled()
        page.unroute(endpoint)
        with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/origin")
        ) as saved:
            # Keyboard submission exercises the same form as a pointer click.
            dialog.get_by_role("button", name="Save", exact=True).focus()
            page.keyboard.press("Enter")
        assert saved.value.ok
        playwright_sync.expect(origin).to_have_text("CD")
        playwright_sync.expect(origin.locator("span")).to_have_attribute(
            "title", "You selected this origin."
        )
        playwright_sync.expect(dialog).to_have_count(0)
        for value, expected in [("Unknown", "Unknown"), ("automatic", initial)]:
            htmx_ready(edit).click()
            select.select_option(value)
            with page.expect_response(
                lambda r: r.request.method == "POST" and r.url.endswith("/origin")
            ) as saved:
                htmx_ready(dialog.get_by_role("button", name="Save", exact=True)).click()
            assert saved.value.ok
            playwright_sync.expect(origin).to_have_text(expected)
        browser.close()


def test_cd_choice_rechecks_media_and_reopens_dismissed_warning(reset_demo_server, htmx_ready):
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"{reset_demo_server}/album/{ALBUM}")
        origin = page.locator("dd[data-field='origin']")
        edit = page.get_by_role("button", name="Change origin", exact=True)
        dialog = page.get_by_role("dialog", name="Change origin")
        warning = page.get_by_text("Matched to a Digital Media release", exact=True)

        def choose(value):
            htmx_ready(edit).click()
            dialog.get_by_label("Origin", exact=True).select_option(value)
            with page.expect_response(
                lambda r: r.request.method == "POST" and r.url.endswith("/origin")
            ):
                htmx_ready(dialog.get_by_role("button", name="Save", exact=True)).click()
            playwright_sync.expect(dialog).to_have_count(0)
            playwright_sync.expect(origin).to_have_text(value)

        # This seeded album has no original UPC. Its confirmed release is digital.
        choose("CD")
        playwright_sync.expect(warning).to_be_visible()
        playwright_sync.expect(
            page.get_by_text("your files are a CD rip.", exact=False)
        ).to_be_visible()
        dismiss = page.get_by_role("checkbox", name="Don't warn me about this")
        with page.expect_response(lambda r: r.url.endswith("/release-accepted")):
            htmx_ready(dismiss).check()
        playwright_sync.expect(warning).to_be_hidden()
        # Unknown suspends checking; returning to CD must assess the new choice.
        choose("Unknown")
        playwright_sync.expect(warning).to_have_count(0)
        choose("CD")
        playwright_sync.expect(warning).to_be_visible()
        playwright_sync.expect(dismiss).not_to_be_checked()
        browser.close()
