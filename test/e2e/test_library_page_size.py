"""Browser tests for the Library's page-size control (#144).

Invisible to the Python suite by construction. The control is a `<select>` inside
a real `<form>` that HTMX intercepts, so everything that can go wrong here is a
question about which handler wins an event — and pytest can only assert that the
attributes are spelled correctly.

That is not hypothetical. The control was first written with
`hx-trigger="change"`, which *replaces* a form's default `submit` trigger rather
than adding to it. Choosing with the mouse worked, so it looked finished; any
submit event reaching the form escaped HTMX entirely and the browser navigated to
`action` — a full page load, with the transient `?anchor=` stranded in the address
bar where a resolved `?page=` belonged. The markup was individually fine, the swap
was individually fine, and the server rendered the right albums either way.

Opt-in: requires playwright (`pip install -e .[e2e]` + `playwright install
chromium`) and RUN_E2E=1. Run via `make e2e`.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1", reason="e2e disabled (set RUN_E2E=1)"
)
playwright_sync = pytest.importorskip("playwright.sync_api")

# The demo library holds a handful of albums, so a page size of 2 is what gives it
# more than one page to move between. It is deliberately off-menu — which also
# exercises the extra <option> the control renders for a size it wasn't offering.
_START = "/?tab=library&page=2&limit=2"

# Survives an in-place swap; a full page load wipes it. That is the entire
# difference these tests are looking for, and neither the DOM nor the rendered
# albums show it — after a native submit the server returns the right page too.
_MARK = "window.__harmonistNoReload = true"


def _open_library_page_two(page: Any, base: str) -> None:
    page.goto(f"{base}{_START}")
    page.wait_for_selector("#library-limit")
    page.evaluate(_MARK)


def test_choosing_a_size_swaps_in_place_and_names_the_resolved_page(demo_server: str) -> None:
    """Changing the size must not reload the page, and must correct the address
    bar to the page the anchor landed on — which only the server knows, so it
    arrives as an HX-Push-Url header rather than a guessed hx-push-url."""
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        _open_library_page_two(page, demo_server)

        page.select_option("#library-limit", "20")
        page.wait_for_function("document.querySelector('#library-total')?.dataset.limit === '20'")

        assert page.evaluate("window.__harmonistNoReload === true"), "the grid reloaded"
        # Row 3 — the top of page 2 at size 2 — sits on page 1 at size 20.
        assert "page=1" in page.url and "limit=20" in page.url
        assert "anchor=" not in page.url, "a transient hint was left in the address bar"

        browser.close()


def test_a_submitted_size_does_not_fall_back_to_a_page_load(demo_server: str) -> None:
    """The regression: HTMX must own the form's `submit`, not just its `change`.

    Driven through `requestSubmit()` rather than a key press. It is the same
    submit event the browser raises when the select is committed from the
    keyboard, and it does not depend on Chrome's rules for which controls trigger
    implicit submission — rules that differ between engines and would make this
    test quietly stop reproducing the bug it exists for.
    """
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        _open_library_page_two(page, demo_server)

        page.eval_on_selector("#library-limit", "s => { s.value = '20'; s.form.requestSubmit(); }")
        page.wait_for_timeout(1000)  # long enough for a navigation to have landed

        assert page.evaluate("window.__harmonistNoReload === true"), (
            "the submit navigated instead of letting HTMX swap"
        )
        assert "anchor=" not in page.url

        browser.close()
