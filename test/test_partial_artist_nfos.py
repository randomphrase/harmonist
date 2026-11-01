"""A failed walk limits conclusions, not the evidence that was read."""

import errno
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from harmonist import artist_nfo, scanner
from test.test_artist_web import ALICE, BOB, SOLO, artist_client, make_album
from test.test_nfo_inventory import write_nfo

__all__ = ["artist_client"]


@pytest.fixture
def partial_library(artist_client, monkeypatch):
    client, cfg, network = artist_client
    root = cfg.paths.music_dir
    blocked = root / "Unavailable"
    blocked.mkdir()
    original_walk = scanner.os.walk
    denied = [True]

    def walk(path, *args, **kwargs):
        for directory, directories, files in original_walk(path, *args, **kwargs):
            if denied[0] and Path(directory) == root and blocked.name in directories:
                directories.remove(blocked.name)
                if callback := kwargs.get("onerror"):
                    callback(PermissionError(errno.EACCES, "Permission denied", str(blocked)))
            yield directory, directories, files

    monkeypatch.setattr(scanner.os, "walk", walk)
    return client, cfg, network, denied


def add_portrait(root):
    nfo = root / "ArtistInfo" / "Different directory name" / "artist.nfo"
    nfo.write_text(nfo.read_text().replace("</artist>", "<thumb>portrait.png</thumb></artist>"))
    image = nfo.parent / "portrait.png"
    image.write_bytes(b"local portrait")
    return nfo, image


def test_partial_walk_preserves_profile_explicit_image_and_revalidation(partial_library):
    client, cfg, network, _ = partial_library
    nfo, image = add_portrait(cfg.paths.music_dir)
    detail = client.get(f"/artist/{ALICE}")
    assert detail.context["nfo"].profile is not None
    assert detail.context["nfo"].profile.path == nfo
    assert "biography" in detail.context["nfo"].profile.biography
    assert "NFO scan incomplete" in detail.text and "Unavailable" in detail.text
    response = client.get(f"/artist/{ALICE}/image")
    assert response.content == image.read_bytes()
    assert (
        client.get(
            f"/artist/{ALICE}/image", headers={"If-None-Match": response.headers["etag"]}
        ).status_code
        == 304
    )
    network.assert_not_called()


@pytest.mark.parametrize("view", ["list", "cards"])
def test_partial_facets_withhold_absence_and_total_claims(partial_library, view):
    client, cfg, _, _ = partial_library
    add_portrait(cfg.paths.music_dir)
    write_nfo(cfg.paths.music_dir, "metadata/orphan")
    response = client.get(f"/artists?view={view}&nfo=missing&artwork=missing")
    data = response.context["artists_view"]
    assert {a.mbid for a in data["rows"]} == {ALICE, BOB}
    assert data["nfo"] is None and data["artwork"] is None
    assert data["nfo_orphans"] is None
    assert all(not badges for badges in data["badges"].values())
    choices = {f["key"]: {o["value"]: o["count"] for o in f["choices"]} for f in data["facets"]}
    assert choices == {
        "nfo": {"with": 1, "missing": None, "conflict": None},
        "artwork": {"with": 1, "missing": None},
    }
    soup = BeautifulSoup(response.text, "html.parser")
    unavailable = soup.select('#artists-nfo-menu [aria-disabled="true"]')
    assert len(unavailable) == 2
    assert all("Unavailable" in option.get_text() for option in unavailable)
    assert "≥ 1" in soup.select_one("#artists-nfo-menu").get_text()
    assert [
        a.mbid for a in client.get("/artists?nfo=with&artwork=with").context["artists_view"]["rows"]
    ] == [ALICE]


def test_partial_walk_reports_observed_duplicates_without_selecting_one(partial_library):
    client, cfg, _, _ = partial_library
    first, _ = add_portrait(cfg.paths.music_dir)
    second = write_nfo(cfg.paths.music_dir, "metadata/duplicate", ALICE)
    detail = client.get(f"/artist/{ALICE}")
    result = detail.context["nfo"]
    assert result.profile is None and set(result.candidates) == {first, second}
    assert "NFO scan incomplete" in detail.text
    assert client.get(f"/artist/{ALICE}/image").status_code == 404
    response = client.get("/artists?nfo=conflict")
    assert response.context["artists_view"]["badges"][ALICE] == ("NFO conflict",)
    group = BeautifulSoup(response.text, "html.parser").select_one(
        '[aria-label="Conflicting NFOs"]'
    )
    assert group.select_one("summary").get_text() == "Observed NFO conflicts"
    assert "Different directory name/artist.nfo" in group.get_text()
    assert "metadata/duplicate/artist.nfo" in group.get_text()


def test_unreadable_xml_withholds_absence_but_keeps_other_profiles(artist_client):
    client, cfg, _ = artist_client
    broken = write_nfo(cfg.paths.music_dir, "metadata/broken", BOB)
    broken.write_text("<artist>")
    assert client.get(f"/artist/{ALICE}").context["nfo"].profile is not None
    view = client.get("/artists?nfo=missing").context["artists_view"]
    assert view["nfo"] is None and len(view["rows"]) == 2
    assert view["nfo_orphans"] is None and view["nfo_errors"]
    write_nfo(cfg.paths.music_dir, "metadata/broken", BOB)
    recovered = client.get("/artists?nfo=missing").context["artists_view"]
    assert recovered["nfo"] == "missing" and recovered["rows"] == []
    assert recovered["nfo_orphans"] == () and not recovered["nfo_errors"]


def test_picture_failure_preserves_biography_and_does_not_imply_absence(artist_client, monkeypatch):
    client, cfg, _ = artist_client
    _, image = add_portrait(cfg.paths.music_dir)
    original = artist_nfo._present

    def present(path):
        if path == image:
            raise PermissionError(errno.EACCES, "Permission denied", str(image))
        return original(path)

    with monkeypatch.context() as patch:
        patch.setattr(artist_nfo, "_present", present)
        detail = client.get(f"/artist/{ALICE}")
        assert detail.context["nfo"].profile.biography
        assert "portrait.png" in " ".join(detail.context["nfo"].errors)
        view = client.get("/artists?artwork=missing").context["artists_view"]
        assert view["artwork"] is None
        assert view["badges"][ALICE] == ()
        assert view["badges"][BOB] == ("Missing NFO", "Missing artwork")
        assert client.get("/artists?nfo=with").context["artists_view"]["nfo_errors"]
    assert client.get(f"/artist/{ALICE}/image").content == image.read_bytes()


def test_partial_scan_does_not_infer_generic_folder_art(partial_library):
    _, cfg, _, _ = partial_library
    root = cfg.paths.music_dir
    make_album(root, "Artist/Album", "66666666-6666-4666-8666-666666666666", ["Bob"], [BOB])
    nfo = write_nfo(root, "Artist", BOB)
    (nfo.parent / "folder.jpg").write_bytes(b"could be an album cover")
    inventory = artist_nfo.scan(root)
    assert inventory.for_artist(BOB).profile is not None
    assert inventory.image_for(BOB) is None


def test_rescan_recovers_coverage_and_checks_inbox_without_stale_flags(
    partial_library, monkeypatch
):
    client, cfg, _, denied = partial_library
    root = cfg.paths.music_dir
    add_portrait(root)
    pending = write_nfo(root, "metadata/pending", SOLO)
    orphan = write_nfo(root, "metadata/no-id")
    runner = client.app.state.scan_runner
    runner.refresh_now()
    monkeypatch.setattr(runner, "is_engaged", lambda: True)
    assert runner.nfo_inventory().for_artist(ALICE).profile is not None
    assert client.get("/artists").context["artists_view"]["nfo_orphans"] is None
    denied[0] = False
    runner.refresh_now()
    view = client.get("/artists?nfo=missing").context["artists_view"]
    assert [a.mbid for a in view["rows"]] == [BOB]
    assert {p.path for p in view["nfo_orphans"]} == {orphan}
    assert pending not in {p.path for p in view["nfo_orphans"]}
    assert not view["nfo_errors"]
    seq = runner.status()["seq"]
    runner.refresh_now()
    assert runner.status()["seq"] == seq
