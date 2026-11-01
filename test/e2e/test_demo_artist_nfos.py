"""The public demo exposes artist profiles and metadata problems offline."""

import os
from urllib.parse import urlsplit

import pytest

from harmonist import demo

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize(
    "engine,width,view", [("chromium", 1280, "list"), ("webkit", 390, "cards")]
)
def test_public_demo_artist_nfos_are_browsable_offline(
    browser_factory, public_demo_server, engine, width, view
):
    base, _root = public_demo_server
    page = browser_factory(engine).new_page(viewport={"width": width, "height": 900})
    blocked = []

    def local_only(route):
        if route.request.url.startswith(base + "/"):
            route.continue_()
        else:
            blocked.append(route.request.url)
            route.abort()

    page.route("**/*", local_only)
    page.goto(f"{base}/artists?view={view}")
    rows = page.get_by_role("list", name="Artists", exact=True)
    pw.expect(rows.get_by_role("link", name="Various Artists", exact=True)).to_have_count(0)
    orphans = page.get_by_role("group", name="NFOs without matching artists")
    pw.expect(orphans.locator("summary")).to_have_text("2 NFOs have no matching artist")
    orphans.locator("summary").click()
    pw.expect(orphans).to_contain_text("The Unidentified Echoes/artist.nfo")
    pw.expect(orphans).to_contain_text("The Unplayed Satellites/artist.nfo")
    pw.expect(orphans.get_by_text("No artist ID", exact=True)).to_have_count(1)
    conflicts = page.get_by_role("group", name="Conflicting NFOs", exact=True)
    conflicts.locator("summary").click()
    pw.expect(conflicts).to_contain_text("ArtistInfo/Mouse Rat/artist.nfo")
    pw.expect(conflicts).to_contain_text("Alternate artist info/Mouse Rat/artist.nfo")

    card = rows.locator(".artist-card").filter(has_text="Barry Jive and the Uptown Five")
    pw.expect(card).to_have_count(1)
    picture = card.locator("img")
    picture.evaluate("e => e.decode()")
    assert picture.evaluate("e => e.naturalWidth > 0")
    card.click()
    pw.expect(
        page.get_by_role("heading", name="Barry Jive and the Uptown Five", exact=True)
    ).to_be_visible()
    biography = page.locator("#artist-biography")
    biography.locator("summary").click()
    pw.expect(biography.locator("p")).to_contain_text("An after-hours soul band")
    pw.expect(page.locator("#artist-detail > header img")).to_be_visible()

    mouse = demo.MB_RELEASES["demo-rel-mouserat"]["artist-credit"][0]["artist"]["id"]
    page.goto(f"{base}/artist/{mouse}")
    pw.expect(page.get_by_role("list", name="Conflicting NFO files").locator("li")).to_have_count(2)
    pw.expect(page.locator("#artist-biography")).to_have_count(0)
    page.goto(f"{base}/?tab=library&q=The%20Rural%20Juror")
    tile = page.locator("#library-grid .lib-tile")
    pw.expect(tile).to_have_count(1)
    pw.expect(tile.locator("div.text-xs")).to_have_text("Various Artists")
    pw.expect(tile.locator(".library-artist-link")).to_have_count(0)
    # Optional web fonts are blocked too; profiles and pictures remain usable.
    assert {urlsplit(url).hostname for url in blocked} <= {
        "fonts.googleapis.com",
        "fonts.gstatic.com",
    }
    page.context.close()
