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
