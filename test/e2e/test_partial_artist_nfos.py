"""Readable artist metadata survives a partial scan and recovers via the watcher."""

import os
import re

import pytest
from test.e2e.conftest import _run_demo_server
from test.test_artist_web import ALICE

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def partial_server(tmp_path_factory):
    root = tmp_path_factory.mktemp("partial-artists")
    for base in _run_demo_server(root, app_module="test.e2e.partial_artists_app:app"):
        yield base, root / "harmonist-demo"


@pytest.mark.parametrize(
    "engine,width,view", [("chromium", 1280, "list"), ("webkit", 390, "cards")]
)
def test_partial_profiles_filters_and_watcher_recovery(
    browser_factory, partial_server, engine, width, view
):
    base, root = partial_server
    marker = root / ".deny-nfo-walk"
    nfo = root / "ArtistInfo" / "unrelated directory name" / "artist.nfo"
    page = browser_factory(engine).new_page(viewport={"width": width, "height": 900})
    try:
        page.goto(f"{base}/artists?artist_q=alice&view={view}&nfo=missing&artwork=missing")
        panel = page.locator("#artists-rows")
        pw.expect(panel.get_by_role("alert").filter(has_text="NFO scan incomplete")).to_be_visible(
            timeout=20000
        )
        card = panel.locator(".artist-card")
        pw.expect(card).to_have_count(1)
        card.locator("img").evaluate("e => e.decode()")
        assert card.locator("img").evaluate("e => e.naturalWidth > 0")
        panel.get_by_role("button", name="NFO filters", exact=True).click()
        options = page.get_by_role("navigation", name="NFO options", exact=True)
        for label in ("Without NFO", "Conflicting NFOs"):
            unavailable = options.locator('[aria-disabled="true"]').filter(has_text=label)
            pw.expect(unavailable).to_contain_text("Unavailable")
        available = options.get_by_role("link", name=re.compile(r"With NFO\s+≥ 1"))
        available.click()
        pw.expect(panel.locator("#artists-page")).to_have_attribute("data-view", view)
        pw.expect(panel.get_by_role("button", name="NFO filters", exact=True)).to_contain_text(
            "≥ 1"
        )
        panel.locator(".artist-card").click()
        pw.expect(page.get_by_role("heading", name="Álice", exact=True)).to_be_visible()
        pw.expect(page.get_by_role("alert").filter(has_text="NFO scan incomplete")).to_be_visible()
        biography = page.locator("#artist-biography")
        biography.locator("summary").click()
        pw.expect(biography.locator("p")).to_have_text("A locally stored biography.")
        pw.expect(page.locator("#library-grid .lib-tile")).to_have_count(2)
        image = page.locator(f'img[src^="/artist/{ALICE}/image"]')
        image.evaluate("e => e.decode()")
        assert image.evaluate("e => e.naturalWidth > 0")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

        marker.unlink()
        nfo.touch()  # A real watcher event requests a full retry of the failed walk.
        page.get_by_role("link", name="← Back to artists", exact=True).click()
        orphans = page.get_by_role("group", name="NFOs without matching artists")
        pw.expect(orphans.locator("summary")).to_have_text(
            "2 NFOs have no matching artist", timeout=20000
        )
        pw.expect(panel.get_by_role("alert")).to_have_count(0)
        panel.get_by_role("button", name="NFO filters", exact=True).click()
        options = page.get_by_role("navigation", name="NFO options", exact=True)
        pw.expect(
            options.locator('[aria-disabled="true"]').filter(has_text="Without NFO")
        ).to_contain_text("0")
        pw.expect(panel.get_by_role("button", name="NFO filters", exact=True)).to_contain_text(
            "With NFO 1"
        )
    finally:
        marker.touch()
        nfo.touch()
        page.context.close()
