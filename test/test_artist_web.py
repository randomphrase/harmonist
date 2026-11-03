"""Artist routes consume local snapshots and NFOs without network enrichment."""

import os
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from mutagen.mp4 import MP4

from harmonist import formats, mb_lookup, sidecar
from harmonist.config import Config, PathsConfig
from harmonist.formats.m4a import ATOM_ALBUM_ARTISTS, ATOM_MB_ALBUM_ARTIST_ID
from harmonist.formats.types import TagSet
from harmonist.models import Sidecar
from harmonist.web.main import create_app

ALICE = "11111111-1111-4111-8111-111111111111"
BOB = "22222222-2222-4222-8222-222222222222"
RELEASE = "33333333-3333-4333-8333-333333333333"
SOLO = "44444444-4444-4444-8444-444444444444"


def make_album(root, directory, release, names, ids, *, confirmed=True):
    folder = root / directory
    folder.mkdir(parents=True)
    path = folder / "01.m4a"
    shutil.copy(Path(__file__).parent / "fixtures" / "sine.m4a", path)
    formats.write_tags(
        path,
        TagSet(
            mb_album_id=release,
            album=directory,
            album_artist=" & ".join(names),
            album_artists=names,
            mb_album_artist_ids=ids,
            title="Song",
            artist="Guest",
            track_num=1,
            track_total=1,
            disc_num=1,
            disc_total=1,
        ),
        None,
    )
    if confirmed:
        sidecar.write(folder, Sidecar(mb_release_id=release))
    return folder


@pytest.fixture
def artist_client(tmp_path, monkeypatch):
    music = tmp_path / "music"
    info = music / "ArtistInfo"
    cfg = Config(paths=PathsConfig(music_dir=music, config_dir=tmp_path / "config"))
    cfg.paths.config_dir.mkdir()
    make_album(music, "Together", RELEASE, ["Alice", "Bob"], [ALICE, BOB])
    make_album(music, "Solo", SOLO, ["Alice"], [ALICE])
    make_album(
        music,
        "Inbox only",
        "55555555-5555-4555-8555-555555555555",
        ["Pending"],
        [SOLO],
        confirmed=False,
    )
    folder = info / "Different directory name"
    folder.mkdir(parents=True)
    (folder / "artist.nfo").write_text(
        f"<artist><name>Alice</name><musicBrainzArtistID>{ALICE}</musicBrainzArtistID>"
        "<biography>&lt;script&gt;biography&lt;/script&gt;</biography><born>1980</born></artist>"
    )
    network = Mock(side_effect=AssertionError("Artist browsing must stay offline"))
    monkeypatch.setattr(mb_lookup, "fetch_release", network)
    return TestClient(create_app(cfg), headers={"HX-Request": "true"}), cfg, network


def test_list_and_artist_page_are_offline_and_share_the_album_tiles(artist_client):
    client, _cfg, network = artist_client
    response = client.get("/?tab=artists")
    assert response.status_code == 200
    assert [a.name for a in response.context["artists_view"]["rows"]] == ["Alice", "Bob"]
    assert response.url.path == "/artists"
    assert (
        BeautifulSoup(response.text, "html.parser").select_one(
            'a[href="/artists"][aria-current="page"]'
        )
        is not None
    )
    assert (
        BeautifulSoup(response.text, "html.parser").select_one("#artists-count").get_text() == "2"
    )
    detail = client.get(f"/artist/{ALICE}?artist_q=Alice&artist_page=2")
    assert detail.status_code == 200
    assert {a.id for a in detail.context["rows"]} == {RELEASE, SOLO}
    assert "&lt;script&gt;biography&lt;/script&gt;" in detail.text
    assert "<script>biography</script>" not in detail.text
    assert f"https://musicbrainz.org/artist/{ALICE}" in detail.text
    assert f"/album/{RELEASE}?from_artist={ALICE}" in detail.text
    bob = client.get(f"/artist/{BOB}")
    assert {a.id for a in bob.context["rows"]} == {RELEASE}
    assert client.get("/artist/unknown").status_code == 404
    assert client.get(f"/artist/{BOB}/image").status_code == 404
    network.assert_not_called()


def test_artist_view_preference_requires_an_explicit_choice_and_urls_override_it(artist_client):
    client, _cfg, network = artist_client
    first = client.get("/artists")
    assert first.context["artists_view"]["view"] == "list"
    assert "harmonist-artists-view" not in client.cookies
    explicit_url = client.get("/artists?view=cards")
    assert explicit_url.context["artists_view"]["view"] == "cards"
    assert "harmonist-artists-view" not in client.cookies
    choice = client.get("/artists?view=cards&remember_view=true", follow_redirects=False)
    assert choice.status_code == 303
    assert "remember_view" not in choice.headers["location"]
    assert client.cookies["harmonist-artists-view"] == "cards"
    assert "HttpOnly" in choice.headers["set-cookie"]
    assert client.get("/artists").context["artists_view"]["view"] == "cards"
    assert client.get("/artists?view=list").context["artists_view"]["view"] == "list"
    assert client.cookies["harmonist-artists-view"] == "cards"
    invalid = client.get("/artists?view=invalid&remember_view=true")
    assert invalid.context["artists_view"]["view"] == "cards"
    assert "set-cookie" not in invalid.headers
    network.assert_not_called()


def test_artist_view_survives_filters_and_artist_album_navigation(artist_client):
    from urllib.parse import parse_qs, urlsplit

    client, _cfg, network = artist_client
    view = client.get("/artists?view=cards&artist_q=Alice&nfo=with").context["artists_view"]
    assert parse_qs(urlsplit(view["clear_url"]).query)["view"] == ["cards"]
    assert parse_qs(urlsplit(view["view_urls"]["list"]).query)["nfo"] == ["with"]
    detail = client.get(view["detail_urls"][ALICE])
    assert parse_qs(urlsplit(detail.context["artist_back_url"]).query)["view"] == ["cards"]
    album_link = BeautifulSoup(detail.text, "html.parser").select_one('#library-grid a[id^="lib-"]')
    assert album_link is not None
    album = client.get(str(album_link["href"]))
    back = client.get(album.context["artist_back_url"])
    assert parse_qs(urlsplit(back.context["artist_back_url"]).query)["view"] == ["cards"]
    network.assert_not_called()


def test_artist_badges_distinguish_conflicts_missing_and_unavailable_inventory(
    artist_client, monkeypatch
):
    from harmonist import artist_nfo

    client, cfg, network = artist_client
    normal = client.get("/artists").context["artists_view"]
    assert normal["badges"][BOB] == ("Missing NFO", "Missing artwork")
    nfo = cfg.paths.music_dir / "Duplicate" / "artist.nfo"
    nfo.parent.mkdir()
    nfo.write_text(f"<artist><musicbrainzartistid>{ALICE}</musicbrainzartistid></artist>")
    conflicted = client.get("/artists").context["artists_view"]
    assert conflicted["badges"][ALICE] == ("NFO conflict",)
    for unavailable in (
        artist_nfo.Inventory(walk_errors=("walk failed",)),
        artist_nfo.Inventory(errors=("read failed",)),
    ):
        monkeypatch.setattr(
            "harmonist.web.main._nfo_inventory", lambda _request, value=unavailable: value
        )
        unknown = client.get("/artists").context["artists_view"]
        assert unknown["badges"][BOB] == ()
        assert unknown["nfo_errors"]
    network.assert_not_called()


def test_album_review_redirect_keeps_the_artist_view():
    from starlette.requests import Request

    from harmonist.web.main import _album_location

    request = Request(
        {
            "type": "http",
            "headers": [
                (
                    b"hx-current-url",
                    b"http://localhost/album/old?from_artist=artist&artist_view=cards&artist_q=Alice",
                )
            ],
        }
    )
    assert "artist_view=cards" in _album_location(request, "new")


def test_search_refresh_and_nfo_errors_recover_without_hiding_albums(artist_client):
    client, cfg, network = artist_client
    found = client.get(
        "/artists?artist_q=bOb&artist_page=999", headers={"X-Harmonist-Push-Url": "1"}
    )
    assert [a.name for a in found.context["artists_view"]["rows"]] == ["Bob"]
    assert found.context["artists_view"]["page"] == 1
    assert BeautifulSoup(found.text, "html.parser").select_one("#artists-count").get_text() == "2"
    assert "artist_q=bOb" in found.headers["HX-Push-Url"]
    path = cfg.paths.music_dir / "ArtistInfo" / "Different directory name" / "artist.nfo"
    original = path.read_bytes()
    path.write_text("<artist>")
    broken = client.get(f"/artist/{ALICE}")
    assert client.get("/artists").context["artists_view"]["nfo_errors"]
    assert len(broken.context["rows"]) == 2
    path.write_bytes(original)
    assert client.get(f"/artist/{ALICE}").context["nfo"].profile is not None
    network.assert_not_called()


def add_unidentified_tracks(folder):
    for i in (2, 3):
        path = folder / f"{i:02d}.m4a"
        shutil.copy(folder / "01.m4a", path)
        audio = MP4(path)
        del audio[ATOM_ALBUM_ARTISTS]
        del audio[ATOM_MB_ALBUM_ARTIST_ID]
        audio.save()


def test_tracks_without_artist_ids_do_not_split_an_identified_artist(artist_client):
    client, cfg, network = artist_client
    add_unidentified_tracks(cfg.paths.music_dir / "Solo")
    before = {p: p.read_bytes() for p in cfg.paths.music_dir.rglob("*") if p.is_file()}
    response = client.get("/artists?artist_q=Alice")
    rows = response.context["artists_view"]["rows"]
    assert [(a.key, len(a.albums)) for a in rows] == [(ALICE, 2)]
    assert [a.title for a in response.context["artists_view"]["missing_id_albums"]] == ["Solo"]
    missing = BeautifulSoup(response.text, "html.parser").select_one(
        '[aria-label="Albums without artist IDs"]'
    )
    assert missing is not None
    assert missing.select_one(f'a[href="/album/{SOLO}"]').get_text() == "Solo"
    detail = client.get(f"/artist/{ALICE}")
    assert {a.id for a in detail.context["rows"]} == {RELEASE, SOLO}
    assert {p: p.read_bytes() for p in before} == before
    network.assert_not_called()


def test_nfo_paths_belong_to_the_artist_with_the_embedded_id(artist_client):
    client, cfg, _network = artist_client
    path = cfg.paths.music_dir / "ArtistInfo" / "Different directory name" / "artist.nfo"
    path.write_text(
        f"<artist><name>Bob</name><musicbrainzartistid>{ALICE.upper()}</musicbrainzartistid></artist>"
    )
    alice = client.get(f"/artist/{ALICE}").context["artist"]
    bob = client.get(f"/artist/{BOB}").context["artist"]
    assert (alice.name, alice.mbid, alice.nfo_paths) == ("Alice", ALICE, (path,))
    assert bob.nfo_paths == ()
    path.write_text("<artist><name>Alice</name></artist>")
    assert client.get(f"/artist/{ALICE}").context["artist"].nfo_paths == ()


def test_missing_artist_ids_link_to_albums_instead_of_creating_local_artists(artist_client):
    client, cfg, _network = artist_client
    make_album(
        cfg.paths.music_dir, "Missing IDs", "66666666-6666-4666-8666-666666666666", ["Alice"], []
    )
    response = client.get("/artists?artist_q=Bob")
    view = response.context["artists_view"]
    assert [a.name for a in view["rows"]] == ["Bob"]
    assert [a.title for a in view["missing_id_albums"]] == ["Missing IDs"]
    assert client.get("/artist/local-" + "0" * 24).status_code == 404


@pytest.mark.parametrize("host", ["library", "heading"])
@pytest.mark.parametrize(
    "display,names,ids,linked",
    [
        ("Alice feat. Bob", ["Alice", "Bob"], [ALICE, BOB], ["Alice", "Bob"]),
        ("Alice with Bob", ["Alice", "Bob"], [ALICE, BOB], ["Alice", "Bob"]),
        ("Alice  &  Bob", ["Alice", "Bob"], [ALICE, BOB], ["Alice", "Bob"]),
        ("Alice / Bob", ["Alice", "Bob"], [ALICE, BOB], ["Alice", "Bob"]),
        ("Alice feat. Bob & <Guest>", ["Alice", "Bob"], [ALICE, BOB], ["Alice", "Bob"]),
        ("Alice feat. Bob", ["Canonical Alice", "Bob"], [ALICE, BOB], []),
        ("Alice & Alice", ["Alice", "Alice"], [ALICE, BOB], []),
        ("Joanne feat. Bob", ["Ann", "Bob"], [ALICE, BOB], []),
        ("Bob feat. Alice", ["Alice", "Bob"], [ALICE, BOB], []),
        ("Alice Cooper feat. Alice", ["Alice Cooper", "Alice"], [ALICE, BOB], []),
        ("AC/DC", ["AC/DC"], [ALICE], ["AC/DC"]),
        ("Alice feat. Bob", ["Alice", "Bob"], [], []),
        (
            "Various Artists feat. Bob",
            ["Various Artists", "Bob"],
            ["89ad4ac3-39f7-470e-963a-56509c546377", BOB],
            ["Bob"],
        ),
    ],
)
def test_album_artist_display_keeps_the_tagged_credit(
    artist_client, host, display, names, ids, linked
):
    client, cfg, network = artist_client
    release = "66666666-6666-4666-8666-666666666666"
    folder = make_album(cfg.paths.music_dir, "Credit display", release, names, ids)
    audio = MP4(folder / "01.m4a")
    audio["aART"] = [display]
    audio.save()
    response = client.get("/library" if host == "library" else f"/album/{release}")
    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    if host == "library":
        credit = (
            soup.select_one(f"#lib-{release}")
            .find_parent(class_="lib-tile")
            .select_one("div.text-xs")
        )
        expected = display
    else:
        credit = soup.select_one("h1 > span.text-muted")
        expected = "— " + display
    assert credit.get_text() == expected
    assert [link.get_text() for link in credit.select("a")] == linked
    for link in credit.select("a"):
        assert link["href"] == f"/artist/{ids[names.index(link.get_text())]}"
    assert credit.find("guest") is None
    network.assert_not_called()


def test_links_within_harmonist_share_one_look_apart_from_musicbrainz(artist_client):
    """#935: a dotted underline is how a link out to MusicBrainz looks, so a name
    that opens Harmonist's own artist or album page is drawn as `app-link`
    wherever it appears."""
    client, cfg, _network = artist_client
    make_album(
        cfg.paths.music_dir, "Missing IDs", "66666666-6666-4666-8666-666666666666", ["Alice"], []
    )
    hosts = {
        "/library": ".lib-tile a[href^='/artist/']",
        f"/artist/{ALICE}": ".lib-tile a[href^='/artist/']",
        f"/album/{RELEASE}": "h1 a[href^='/artist/']",
        "/artists": "details[aria-label='Albums without artist IDs'] a[href^='/album/']",
    }
    for url, selector in hosts.items():
        soup = BeautifulSoup(client.get(url).text, "html.parser")
        links = soup.select(selector)
        assert links, url
        assert all("app-link" in link["class"] for link in links), url


def test_artists_is_a_standalone_page_with_its_own_filters(artist_client):
    client, _cfg, _network = artist_client
    response = client.get("/artists")
    soup = BeautifulSoup(response.text, "html.parser")
    assert soup.html is not None
    assert soup.select_one('[aria-label="Artist filters"]') is not None
    assert soup.select_one('[aria-label="Library filters"]') is None
    link = BeautifulSoup(client.get("/").text, "html.parser").select_one('a[href="/artists"]')
    assert link is not None and "Artists" in link.get_text()


def test_artist_filters_combine_and_new_means_any_recent_album(artist_client):
    client, cfg, network = artist_client
    for folder, age in (("Together", 30), ("Solo", 1)):
        path = cfg.paths.music_dir / folder
        current = sidecar.read(path)
        assert current is not None
        sidecar.write(path, replace(current, added_at=datetime.now(UTC) - timedelta(days=age)))
    picture = cfg.paths.music_dir / "ArtistInfo" / "Different directory name" / "artist.png"
    picture.write_bytes(b"local-artist-image")
    response = client.get("/artists?nfo=with&artwork=with&new=true&artist_q=alice")
    view = response.context["artists_view"]
    assert [a.mbid for a in view["rows"]] == [ALICE]
    assert view["count"] == 2
    missing = client.get("/artists?nfo=missing&artwork=missing").context["artists_view"]
    assert [a.mbid for a in missing["rows"]] == [BOB]
    assert [a.mbid for a in client.get("/artists?new=true").context["artists_view"]["rows"]] == [
        ALICE
    ]
    detail = client.get(f"/artist/{ALICE}?artist_nfo=with&artist_artwork=with&artist_new=true")
    back = detail.context["artist_back_url"]
    assert back.startswith("/artists?")
    assert "nfo=with" in back and "artwork=with" in back and "new=true" in back
    network.assert_not_called()


def test_artwork_filters_use_artist_pictures_and_revalidate_changed_images(artist_client):
    client, cfg, _ = artist_client
    album = cfg.paths.music_dir / "Together"
    (album / "artist.nfo").write_text(
        f"<artist><musicbrainzartistid>{BOB}</musicbrainzartistid></artist>"
    )
    (album / "folder.jpg").write_bytes(b"album-cover")
    assert client.get("/artist/" + BOB + "/image").status_code == 404
    assert [
        a.mbid for a in client.get("/artists?artwork=with").context["artists_view"]["rows"]
    ] == []
    image = album / "artist.png"
    image.write_bytes(b"first-photo")
    response = client.get(f"/artist/{BOB}/image")
    assert response.content == b"first-photo"
    assert response.headers["cache-control"] == "no-cache"
    etag = response.headers["etag"]
    assert client.get(f"/artist/{BOB}/image", headers={"If-None-Match": etag}).status_code == 304
    before = image.stat()
    image.write_bytes(b"other-photo")
    os.utime(image, ns=(before.st_atime_ns, before.st_mtime_ns))
    changed = client.get(f"/artist/{BOB}/image", headers={"If-None-Match": etag})
    assert changed.status_code == 200 and changed.content == b"other-photo"
    assert [
        a.mbid for a in client.get("/artists?artwork=with").context["artists_view"]["rows"]
    ] == [BOB]
