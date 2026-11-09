"""A filter chip that submits a form lines up with the menu chip beside it (#991).

Where a chip sits within its row is decided by layout, which only a browser does:
the rendered markup is identical whether or not the two line up.
"""

import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize(
    ("path", "row_name"),
    [("/?tab=library", "Library filters"), ("/artists", "Artist filters")],
)
def test_form_chip_lines_up_with_menu_chip(
    browser_factory, public_demo_server, engine, path, row_name
):
    base, _root = public_demo_server
    browser = browser_factory(engine)
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(f"{base}{path}")
    row = page.get_by_role("navigation", name=row_name, exact=True)

    menu_chip = row.locator(":scope > button").first.bounding_box()
    form_chip = row.locator(":scope > form > button").first.bounding_box()

    assert form_chip["y"] == pytest.approx(menu_chip["y"], abs=0.25)
    assert form_chip["height"] == pytest.approx(menu_chip["height"], abs=0.25)
