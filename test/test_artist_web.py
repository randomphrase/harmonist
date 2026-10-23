"""Artist routes consume local snapshots and NFOs without network enrichment."""

import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from mutagen.mp4 import MP4

from harmonist import formats, mb_lookup, sidecar
from harmonist.config import Config, PathsConfig, load
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
    info = tmp_path / "ArtistInfo"
    cfg = Config(
        paths=PathsConfig(music_dir=music, config_dir=tmp_path / "config", artist_info_dir=info)
    )
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
    assert response.context["initial_tab"] == "artists"
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
    assert "artist_q=bOb" in found.headers["HX-Push-Url"]
    path = cfg.paths.artist_info_dir / "Different directory name" / "artist.nfo"
    original = path.read_bytes()
    path.write_text("<artist>")
    broken = client.get(f"/artist/{ALICE}")
    assert broken.context["nfo"].errors
    assert len(broken.context["rows"]) == 2
    path.write_bytes(original)
    assert client.get(f"/artist/{ALICE}").context["nfo"].profile is not None
    network.assert_not_called()


def test_artist_information_folder_environment_and_demo_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("HARMONIST_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("HARMONIST_MUSIC_DIR", str(tmp_path / "music"))
    monkeypatch.setenv("HARMONIST_ARTIST_INFO_DIR", str(tmp_path / "Artists"))
    monkeypatch.setenv("HARMONIST_DEMO_MODE", "0")
    assert load().paths.artist_info_dir == tmp_path / "Artists"
    monkeypatch.setenv("HARMONIST_DEMO_MODE", "1")
    assert load().paths.artist_info_dir is None


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
    assert [a.title for a in rows[0].unidentified_albums] == ["Solo"]
    assert "Unidentified credit - Solo" in BeautifulSoup(response.text, "html.parser").get_text()
    detail = client.get(f"/artist/{ALICE}")
    assert {a.id for a in detail.context["rows"]} == {RELEASE, SOLO}
    assert "Unidentified credit - Solo" in BeautifulSoup(detail.text, "html.parser").get_text()
    assert {p: p.read_bytes() for p in before} == before
    network.assert_not_called()
