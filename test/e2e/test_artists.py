"""Artists navigation must work in real browsers, including HTMX and Back."""

import os
import re
import shutil
from pathlib import Path

import pytest
from test.e2e.conftest import _run_demo_server
from test.test_artist_web import ALICE, BOB, RELEASE, make_album
from test.test_nfo_inventory import write_nfo

from harmonist import demo

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def artists_root(tmp_path_factory):
    return tmp_path_factory.mktemp("artists")


@pytest.fixture(scope="module")
def artists_server(artists_root):
    yield from _run_demo_server(artists_root, app_module="test.e2e.artists_app:app")


@pytest.mark.parametrize("engine,width", [("chromium", 1280), ("webkit", 390)])
def test_artist_nfo_sections_and_disclosures_survive_background_refresh(
    browser_factory, artists_server, engine, width
):
    page = browser_factory(engine).new_page(viewport={"width": width, "height": 900})
    page.goto(artists_server + f"/artist/{ALICE}")
    biography = page.locator("#artist-biography")
    discography = page.locator("#artist-discography")
    pw.expect(biography.locator("summary")).to_have_text("Biography")
    pw.expect(biography).to_have_js_property("open", False)
    pw.expect(biography.locator("p")).not_to_be_visible()
    biography.locator("summary").focus()
    biography.locator("summary").press("Enter")
    pw.expect(biography.locator("p")).to_be_visible()
    pw.expect(discography).to_have_js_property("open", False)
    discography.locator("summary").click()
    releases = page.get_by_role("list", name="NFO discography").locator("li")
    pw.expect(releases).to_have_count(2)
    pw.expect(
        releases.first.get_by_role("link", name="Unowned studio album", exact=False)
    ).to_have_attribute("href", f"https://musicbrainz.org/release-group/{BOB}")
    pw.expect(releases.last).to_contain_text("<script>Other album</script>")
    pw.expect(releases.last.get_by_role("link")).to_have_count(0)
    pw.expect(page.get_by_role("list", name="Artist genres").locator("li")).to_have_text(
        ["Rock", "Alternative"]
    )
    pw.expect(page.get_by_role("region", name="Additional information")).to_contain_text("Indie")
    pw.expect(page.get_by_role("region", name="Albums 2").locator(".lib-tile")).to_have_count(2)
    detail = page.locator("#artist-detail")
    detail.evaluate("e => e.dataset.beforeRefresh = 'yes'")
    with page.expect_response(lambda r: f"/artist/{ALICE}" in r.url and r.ok):
        page.evaluate("htmx.trigger(document.body, 'tasks-changed')")
    pw.expect(detail).not_to_have_attribute("data-before-refresh", "yes")
    pw.expect(biography).to_have_js_property("open", True)
    pw.expect(discography).to_have_js_property("open", True)
    biography.locator("summary").click()
    detail.evaluate("e => e.dataset.beforeRefresh = 'yes'")
    with page.expect_response(lambda r: f"/artist/{ALICE}" in r.url and r.ok):
        page.evaluate("htmx.trigger(document.body, 'tasks-changed')")
    pw.expect(detail).not_to_have_attribute("data-before-refresh", "yes")
    pw.expect(biography).to_have_js_property("open", False)
    pw.expect(discography).to_have_js_property("open", True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.context.close()


@pytest.mark.parametrize("engine,width", [("chromium", 1280), ("webkit", 390)])
def test_library_cards_link_artists_independently_after_paging(
    browser_factory, artists_server, htmx_ready, engine, width
):
    page = browser_factory(engine).new_page(viewport={"width": width, "height": 900})
    page.goto(artists_server + "/?tab=library&limit=1&q=alice")
    panel = page.locator("#panel-library")
    with page.expect_response(lambda r: "/library?" in r.url and r.ok):
        htmx_ready(panel.get_by_role("link", name="Next →", exact=True)).click()
    pw.expect(page.locator("#library-page")).to_have_attribute("data-page", "2")
    origin = page.url
    tile = page.locator("#library-grid .lib-tile")
    album = page.locator('#library-grid a[id^="lib-"]')
    album_url = album.get_attribute("href")
    pw.expect(album.locator("img")).to_have_count(1)
    artist_link = tile.get_by_role("link", name="Álice", exact=True)
    pw.expect(artist_link).to_have_attribute("href", f"/artist/{ALICE}")
    muted = tile.locator(".text-xs").evaluate("e => getComputedStyle(e).color")
    pw.expect(artist_link).to_have_css("color", muted)
    artist_link.hover()
    hover_color = artist_link.evaluate("e => getComputedStyle(e).color")
    assert hover_color != muted
    artist_link.focus()
    artist_link.press("Enter")
    page.wait_for_url(f"**/artist/{ALICE}")
    pw.expect(page.get_by_role("heading", name="Álice", exact=True)).to_be_visible()
    page.go_back()
    pw.expect(page).to_have_url(origin)
    album.click()
    page.wait_for_url(artists_server + album_url)
    back = page.get_by_role("link", name="← Back to library", exact=True)
    back.hover()
    pw.expect(back).to_have_css("color", hover_color)
    back.click()
    pw.expect(page.locator("#library-page")).to_have_attribute("data-page", "2")
    pw.expect(panel.get_by_role("searchbox", name="Search the library")).to_have_value("alice")
    pw.expect(panel.locator('#library-grid a[id^="lib-"]')).to_have_count(1)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.context.close()


@pytest.mark.parametrize("engine,width", [("chromium", 1280), ("webkit", 390)])
def test_album_heading_links_each_artist_with_muted_dotted_underlines(
    browser_factory, artists_server, engine, width
):
    page = browser_factory(engine).new_page(viewport={"width": width, "height": 900})
    page.goto(artists_server + f"/album/{RELEASE}")
    heading = page.locator("#album-identity h1")
    pw.expect(heading).to_have_text("Unstructured collaboration — Álice & Zed")
    for name, identity in (("Álice", ALICE), ("Zed", BOB)):
        link = heading.get_by_role("link", name=name, exact=True)
        pw.expect(link).to_have_attribute("href", f"/artist/{identity}")
        page.mouse.move(0, 0)
        pw.expect(link).to_have_css("text-decoration-style", "dotted")
        assert link.evaluate("e => getComputedStyle(e).color") == heading.locator(
            ".text-muted"
        ).evaluate("e => getComputedStyle(e).color")
        link.hover()
        pw.expect(link).to_have_css("text-decoration-style", "solid")
        link.focus()
        link.press("Enter")
        page.wait_for_url(f"**/artist/{identity}")
        pw.expect(page.get_by_role("heading", name=name, exact=True)).to_be_visible()
        page.go_back()
        pw.expect(heading).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.context.close()


@pytest.mark.parametrize("engine,width", [("chromium", 1280), ("webkit", 390)])
def test_artist_search_paging_details_and_album_return(
    browser_factory, artists_server, htmx_ready, engine, width
):
    base = artists_server
    page = browser_factory(engine).new_page(viewport={"width": width, "height": 900})
    page.goto(base + "/?tab=library")
    page.get_by_role("navigation", name="Main navigation").locator('a[href="/artists"]').click()
    page.wait_for_url("**/artists")
    panel = page.locator("#artists-rows")
    pw.expect(panel).to_be_visible()
    count = page.locator("#artists-count")
    total_artists = count.inner_text()
    assert int(total_artists) > 30
    cards = panel.get_by_role("list", name="Artists").locator("li > a")
    heights = cards.evaluate_all("cards => cards.map(c => c.getBoundingClientRect().height)")
    assert len(heights) > 3
    assert max(heights) - min(heights) < 1
    names = panel.get_by_role("list", name="Artists").locator("li a .font-bold").all_text_contents()
    assert names[0] == "Álice"
    missing_ids = panel.get_by_role("group", name="Albums without artist IDs")
    missing_ids.locator("summary").click()
    missing_album = missing_ids.get_by_role("link", name="Another arbitrary folder", exact=True)
    pw.expect(missing_album).to_have_attribute(
        "href", "/album/44444444-4444-4444-8444-444444444444"
    )
    thumbnail = panel.locator(f'img[src^="/artist/{ALICE}/image"]')
    pw.expect(thumbnail).to_be_visible()
    page.wait_for_function(
        "src => document.querySelector(`img[src^='${src}']`)?.naturalWidth > 0",
        arg=f"/artist/{ALICE}/image",
    )
    # An artist without a local image keeps the icon without making a doomed request.
    missing = panel.locator('a[href^="/artist/22222222-2222-4222-8222-222222222222?"]')
    pw.expect(missing.locator("svg")).to_be_visible()
    with page.expect_response(lambda r: "/artists?" in r.url and r.ok):
        htmx_ready(panel.get_by_role("link", name="Next →")).click()
    pw.expect(panel.locator("#artists-page")).to_have_attribute("data-page", "2")
    page.reload()
    pw.expect(panel).to_be_visible()
    pw.expect(panel.locator("#artists-page")).to_have_attribute("data-page", "2")
    search = panel.get_by_role("searchbox", name="Search artists")
    search.fill("alice")
    with page.expect_response(lambda r: "/artists?" in r.url and r.ok):
        htmx_ready(panel.get_by_role("button", name="Search", exact=True)).click()
    pw.expect(panel.get_by_role("list", name="Artists").locator("li")).to_have_count(1)
    pw.expect(count).to_have_text(total_artists)
    link = panel.get_by_role("link", name="Álice 2 albums", exact=False)
    link.focus()
    link.press("Enter")
    page.wait_for_url(f"**/artist/{ALICE}?**")
    pw.expect(page.get_by_role("heading", name="Álice", exact=True)).to_be_visible()
    pw.expect(page.locator("#artist-biography")).to_contain_text("A locally stored biography.")
    pw.expect(page.locator("#library-grid .lib-tile")).to_have_count(2)
    image = page.get_by_role("img", name="Álice", exact=True)
    pw.expect(image).to_be_visible()
    page.wait_for_function("document.querySelector('img[alt=\"Álice\"]').naturalWidth > 0")
    page.locator('#library-grid a[id^="lib-"]').first.click()
    page.get_by_role("link", name="← Back to artist", exact=True).click()
    pw.expect(page.get_by_role("heading", name="Álice", exact=True)).to_be_visible()
    page.get_by_role("link", name="← Back to artists", exact=True).click()
    pw.expect(panel).to_be_visible()
    pw.expect(panel.get_by_role("searchbox", name="Search artists")).to_have_value("alice")
    pw.expect(panel.get_by_role("list", name="Artists").locator("li")).to_have_count(1)
    # The browser's history path must also retain the artist and query.
    page.go_back()
    pw.expect(page.get_by_role("heading", name="Álice", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.context.close()


def test_artist_count_refreshes_when_the_watcher_discovers_an_artist(
    browser_factory, artists_server, artists_root
):
    page = browser_factory("chromium").new_page()
    page.goto(artists_server + "/artists?artist_q=alice")
    count = page.locator("#artists-count")
    total = int(count.inner_text())
    pw.expect(page.get_by_role("list", name="Artists").locator("li")).to_have_count(1)
    make_album(
        artists_root / "harmonist-demo",
        "New counted artist",
        "77777777-7777-4777-8777-777777777777",
        ["New counted artist"],
        ["66666666-6666-4666-8666-666666666666"],
    )
    pw.expect(count).to_have_text(str(total + 1), timeout=20000)
    pw.expect(page.get_by_role("list", name="Artists").locator("li")).to_have_count(1)
    page.context.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_nfo_watcher_refreshes_conflicts_orphans_and_metadata(
    browser_factory, artists_server, artists_root, engine
):
    root = artists_root / "harmonist-demo"
    page = browser_factory(engine).new_page(viewport={"width": 390, "height": 900})
    page.goto(artists_server + f"/artist/{ALICE}")
    metadata = page.locator("#artist-biography")
    pw.expect(metadata).to_contain_text("A locally stored biography.")
    duplicate = write_nfo(root, f"metadata/{engine}/duplicate", ALICE)
    conflicts = page.get_by_role("list", name="Conflicting NFO files")
    pw.expect(conflicts.locator("li")).to_have_count(2, timeout=20000)
    pw.expect(conflicts).to_contain_text(f"metadata/{engine}/duplicate/artist.nfo")
    duplicate.unlink()
    pw.expect(metadata).to_contain_text("A locally stored biography.", timeout=20000)

    page.goto(artists_server + "/artists")
    orphan = write_nfo(root, f"metadata/{engine}/orphan")
    orphans = page.get_by_role("group", name="NFOs without matching artists")
    pw.expect(orphans).to_be_visible(timeout=20000)
    orphans.locator("summary").click()
    pw.expect(orphans).to_contain_text(f"metadata/{engine}/orphan/artist.nfo")
    orphan.write_text("<artist>")
    errors = page.get_by_role("region", name="NFO read errors")
    pw.expect(errors).to_contain_text(f"metadata/{engine}/orphan/artist.nfo", timeout=20000)
    orphan.unlink()
    pw.expect(errors).to_have_count(0, timeout=20000)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.context.close()


@pytest.mark.parametrize("engine,width", [("chromium", 1280), ("webkit", 390)])
def test_artist_filters_survive_reload_and_the_album_round_trip(
    browser_factory, artists_server, htmx_ready, engine, width
):
    page = browser_factory(engine).new_page(viewport={"width": width, "height": 900})
    page.goto(artists_server + "/artists?artist_q=alice")
    for title, choice in (("NFO", "With NFO"), ("Artwork", "With artwork")):
        button = page.get_by_role("button", name=title + " filters", exact=True)
        button.click()
        option = page.get_by_role("link", name=re.compile("^" + choice))
        with page.expect_response(lambda response: "/artists?" in response.url and response.ok):
            htmx_ready(option).click()
        pw.expect(button).to_contain_text(choice)
    new = page.get_by_role("button", name=re.compile("^New"))
    with page.expect_response(lambda response: "/artists?" in response.url and response.ok):
        htmx_ready(new).click()
    pw.expect(page.get_by_role("button", name=re.compile("New"))).to_have_attribute(
        "aria-pressed", "true"
    )
    page.reload()
    pw.expect(page.get_by_role("button", name="NFO filters")).to_contain_text("With NFO")
    pw.expect(page.get_by_role("button", name="Artwork filters")).to_contain_text("With artwork")
    pw.expect(page.get_by_role("searchbox", name="Search artists")).to_have_value("alice")
    page.locator(f'a[href^="/artist/{ALICE}?"]').click()
    pw.expect(page.get_by_role("heading", name="Álice", exact=True)).to_be_visible()
    page.locator('#library-grid a[id^="lib-"]').first.click()
    page.get_by_role("link", name="← Back to artist", exact=True).click()
    page.get_by_role("link", name="← Back to artists", exact=True).click()
    pw.expect(page.get_by_role("button", name="NFO filters")).to_contain_text("With NFO")
    pw.expect(page.get_by_role("button", name="Artwork filters")).to_contain_text("With artwork")
    pw.expect(page.get_by_role("button", name=re.compile("New"))).to_have_attribute(
        "aria-pressed", "true"
    )
    search = page.get_by_role("searchbox", name="Search artists")
    search.fill("no such artist")
    with page.expect_response(lambda response: "/artists?" in response.url and response.ok):
        htmx_ready(page.get_by_role("button", name="Search", exact=True)).click()
    pw.expect(page.get_by_role("list", name="Artists").locator("li > a")).to_have_count(0)
    page.get_by_role("link", name="Clear filters", exact=True).click()
    pw.expect(page.get_by_role("button", name="NFO filters")).to_contain_text("NFO")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.context.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_artwork_filter_and_open_menu_follow_local_picture_changes(
    browser_factory, artists_server, artists_root, engine
):
    root = artists_root / "harmonist-demo"
    nfo = write_nfo(root, f"metadata/artwork-live/{engine}", "22222222-2222-4222-8222-222222222222")
    page = browser_factory(engine).new_page(viewport={"width": 390, "height": 900})
    page.goto(artists_server + "/artists?artwork=with&artist_q=zed")
    rows = page.get_by_role("list", name="Artists").locator("li > a")
    pw.expect(rows).to_have_count(0)
    image = nfo.parent / "artist.png"
    shutil.copy(Path(demo.ASSETS_DIR) / "wyld.png", image)
    pw.expect(rows).to_have_count(1, timeout=20000)
    picture = rows.locator("img")
    pw.expect(picture).to_be_visible()
    page.wait_for_function("document.querySelector('#artists-page img')?.naturalWidth > 0")
    page.get_by_role("button", name="Artwork filters", exact=True).click()
    menu = page.locator("#artists-artwork-menu:popover-open")
    pw.expect(menu).to_be_visible()
    image.unlink()
    pw.expect(rows).to_have_count(0, timeout=20000)
    pw.expect(menu).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    nfo.unlink()
    page.context.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_declared_thumb_and_profile_fallbacks_refresh_without_an_nfo_edit(
    browser_factory, artists_server, artists_root, engine
):
    root = artists_root / "harmonist-demo"
    number = "1" if engine == "chromium" else "2"
    identity = "bbbbbbbb-bbbb-4bbb-8bbb-" + number * 12
    release = "aaaaaaaa-aaaa-4aaa-8aaa-" + number * 12
    name = "Picture fixture " + engine
    make_album(root, name + "/Album", release, [name], [identity])
    nfo = write_nfo(root, name, identity)
    nfo.write_text(
        f"<artist><musicbrainzartistid>{identity}</musicbrainzartistid><thumb>photos/portrait.png</thumb></artist>"
    )
    before = nfo.read_bytes(), nfo.stat().st_mtime_ns
    preferred = nfo.parent / "photos" / "portrait.png"
    preferred.parent.mkdir()
    fallback = nfo.parent / "artist.png"
    generic = nfo.parent / "folder.png"
    preferred_bytes = (Path(demo.ASSETS_DIR) / "wyld.png").read_bytes()
    fallback_bytes = (Path(demo.ASSETS_DIR) / "mouse.png").read_bytes()
    preferred.write_bytes(preferred_bytes)
    fallback.write_bytes(fallback_bytes)
    generic.write_bytes(preferred_bytes)
    page = browser_factory(engine).new_page()
    page.goto(artists_server + "/artists?artist_q=Picture+fixture")
    link = page.locator(f'a[href^="/artist/{identity}?"]')
    pw.expect(link).to_be_visible(timeout=20000)
    link.click()
    image_url = artists_server + f"/artist/{identity}/image"
    picture = page.get_by_role("img", name=name, exact=True)
    pw.expect(picture).to_be_visible()
    page.wait_for_function(
        "name => { const image = [...document.images].find(i => i.alt === name); "
        "return image?.complete && image.naturalWidth > 0; }",
        arg=name,
    )
    initial = page.request.get(image_url)
    assert initial.body() == preferred_bytes
    assert (
        page.request.get(image_url, headers={"If-None-Match": initial.headers["etag"]}).status
        == 304
    )
    for path, expected in ((preferred, fallback_bytes), (fallback, preferred_bytes)):
        with page.expect_response(
            lambda response: response.url.partition("?")[0] == image_url and response.status == 200,
            timeout=20000,
        ) as replacement:
            path.unlink()
        assert replacement.value.body() == expected
    generic.unlink()
    pw.expect(picture).to_have_count(0, timeout=20000)
    assert (nfo.read_bytes(), nfo.stat().st_mtime_ns) == before
    page.context.close()
