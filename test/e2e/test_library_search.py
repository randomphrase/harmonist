"""Browser tests for the Library's search box (#180).

Two behaviors here that the Python suite cannot see, both of the same family as
#144's: they are questions about which handler wins an event, and about DOM
identity across a swap. pytest sees a correct-looking string either way.

1. **`hx-preserve` keeps a half-typed query.** The search form lives inside
   `#library-rows`, which is replaced wholesale on every `tasks-changed` and
   `library-refresh` — and this app runs background scans on a timer, so that
   fires while people are typing. Without `hx-preserve` the swap rebuilds the
   input from the server's `value`, silently reverting whatever was being typed
   to the last *submitted* query. Rendered HTML is identical in both cases; only
   node identity differs.

2. **HTMX owns the form's `submit`.** `hx-trigger="submit, search"` names both,
   because naming only `search` would REPLACE the default rather than extend it
   and send every Enter into a native navigation to `action`. That fallback
   renders the right albums, so nothing looks broken — it is just a full page
   load with the address bar left one state behind.

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

# Survives an in-place swap; a full page load wipes it. Same trick as the
# page-size tests, and for the same reason: after a native submit the server
# returns the right albums too, so the rendered page cannot tell you which
# happened.
_MARK = "window.__harmonistNoReload = true"


def _open_library(page: Any, base: str, query: str = "") -> None:
    page.goto(f"{base}/?tab=library{query}")
    page.wait_for_selector("#library-search")
    page.evaluate(_MARK)


def test_a_background_refresh_does_not_eat_a_half_typed_query(demo_server: str) -> None:
    """The grid re-requests itself whenever a scan or sync finishes. Someone
    mid-word when that lands must not have their query replaced by the last one
    they submitted."""
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        # Start from a SUBMITTED search, so the server's `value` and the typed
        # text differ — otherwise a rebuilt input is indistinguishable from a
        # preserved one and the test cannot fail.
        _open_library(page, demo_server, "&q=blues")
        page.fill("#library-search", "half-typed quer")

        page.evaluate("document.body.dispatchEvent(new Event('library-refresh'))")
        # Long enough for the swap to have landed and settled.
        page.wait_for_timeout(1200)

        assert page.input_value("#library-search") == "half-typed quer", (
            "the background refresh reverted the box to the submitted query"
        )
        assert page.evaluate("window.__harmonistNoReload === true"), "the page reloaded"

        browser.close()


def test_a_submitted_search_does_not_fall_back_to_a_page_load(demo_server: str) -> None:
    """The #144 regression class, on a second form. Driven through
    `requestSubmit()` rather than an Enter key press: it raises the same submit
    event the browser does, without depending on an engine's rules for implicit
    submission — rules that differ per browser and would let this test quietly
    stop reproducing its bug."""
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        _open_library(page, demo_server)

        page.fill("#library-search", "blues")
        page.eval_on_selector("#library-search", "i => i.form.requestSubmit()")
        page.wait_for_timeout(1200)  # long enough for a navigation to have landed

        assert page.evaluate("window.__harmonistNoReload === true"), (
            "the submit navigated instead of letting HTMX swap"
        )
        # And the server named the resolved view, which the form could not have
        # spelled itself — it knows neither the page size nor the landing page.
        assert "q=blues" in page.url and "limit=" in page.url

        browser.close()
