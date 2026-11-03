"""The main navigation's tabs stay inside it, so it has nothing to scroll
vertically (#944).

The nav scrolls sideways on a narrow screen (`overflow-x-auto`), which makes
CSS compute `overflow-y: auto` too. Tabs pulled 1px down onto the nav's border
overhung it by that pixel, and the nav grew a vertical scrollbar. Only layout
shows the overhang; `test_web.py` sees the same class list either way.

`scrollHeight` counts overflow whether or not it is clipped, so this also fails
for a fix that hides the overhang (`overflow-y-hidden`) by cutting a pixel off
the active tab's underline.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1", reason="e2e disabled (set RUN_E2E=1)"
)
playwright_sync = pytest.importorskip("playwright.sync_api")

NAV = "nav[aria-label='Main navigation']"


# `/` renders the tabs as buttons, `/artists` as links: both share the overhang.
@pytest.mark.parametrize("path", ["/", "/artists"])
def test_main_navigation_has_no_vertical_overflow(
    browser_factory, demo_server: str, path: str
) -> None:
    page = browser_factory("chromium").new_page(viewport={"width": 1280, "height": 800})
    page.goto(f"{demo_server}{path}")
    nav = page.locator(NAV)
    playwright_sync.expect(nav).to_be_visible()

    scroll, client = nav.evaluate("n => [n.scrollHeight, n.clientHeight]")
    assert scroll <= client, f"the nav overflows by {scroll - client}px"

    page.context.close()
