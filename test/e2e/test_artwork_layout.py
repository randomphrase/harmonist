"""Reviewed artwork applies directly and shares space with its picker (#683)."""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1", reason="e2e disabled (set RUN_E2E=1)"
)
playwright_sync = pytest.importorskip("playwright.sync_api")

ALBUM_ID = "demo-rel-dingoes"


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_apply_uses_section_footer_within_picker_reservation(demo_server: str, engine: str) -> None:
    with playwright_sync.sync_playwright() as pw:
        browser = getattr(pw, engine).launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1400})
        page.goto(f"{demo_server}/album/{ALBUM_ID}")
        page.wait_for_selector("#album-artwork .art-pick__picker")
        page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")
        section = page.locator("#album-artwork")
        section.scroll_into_view_if_needed()
        rows = section.locator(".art-row")
        last = rows.last.bounding_box()
        button = section.get_by_role("button", name="Apply this album's artwork")
        apply = button.bounding_box()
        panel = section.bounding_box()
        assert last and apply and panel
        assert apply["y"] >= last["y"] + last["height"]
        assert 0 <= panel["y"] + panel["height"] - (apply["y"] + apply["height"]) <= 32, (
            "Apply belongs at the foot of the whole section"
        )

        rows.last.locator(".art-row__select").click()
        picker = section.locator(".art-pick__picker").bounding_box()
        panel = section.bounding_box()
        assert picker and panel
        assert picker["y"] + picker["height"] <= panel["y"] + panel["height"]
        after = button.bounding_box()
        last_after = rows.last.bounding_box()
        assert after and last_after
        assert abs((after["y"] - last_after["y"]) - (apply["y"] - last["y"])) < 1, (
            "moving the picker moved Apply relative to its rows"
        )
        assert panel["y"] + panel["height"] - (picker["y"] + picker["height"]) <= 48, (
            "Apply must fit beside the picker without extending the section"
        )

        for width in (760, 390):
            page.set_viewport_size({"width": width, "height": 1400})
            picker = section.locator(".art-pick__picker").bounding_box()
            last = rows.last.bounding_box()
            apply = button.bounding_box()
            assert picker and last and apply
            assert picker["y"] >= last["y"] + last["height"] - 1
            assert 0 <= apply["y"] - (picker["y"] + picker["height"]) <= 32
            assert apply["x"] + apply["width"] <= width
        browser.close()


def test_reviewed_artwork_applies_with_one_press(reset_demo_server: str) -> None:
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(f"{reset_demo_server}/album/{ALBUM_ID}")
        page.wait_for_selector("#album-artwork .art-pick__picker")
        page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")
        section = page.locator("#album-artwork")
        # Explicitly replace a row that already has embedded artwork. The
        # initial suggestion can be additions only, which never asked twice.
        first = section.locator(".art-row").first
        assert first.locator("button.art-row__art").count() >= 1
        first.locator(".art-row__select").click()
        with page.expect_response(re.compile(r"/artwork\?")):
            section.locator(".art-pick__use--go:visible").click()
        page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")
        # Playwright dismisses unexpected dialogs by default. A single press
        # must reach the write and resolve the reviewed plan without accepting one.
        with page.expect_response(re.compile(r"/artwork/update$"), timeout=5000) as answer:
            section.get_by_role("button", name="Apply this album's artwork").click()
        assert answer.value.ok
        page.wait_for_function(
            """() => !document.querySelector(
                '#album-artwork form[hx-post$="/artwork/update"]')"""
        )
        browser.close()
