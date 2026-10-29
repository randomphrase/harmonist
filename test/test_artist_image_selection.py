"""Artist pictures require an explicit reference or unambiguous folder evidence."""

from pathlib import Path
from unittest.mock import Mock

import pytest
from mutagen.mp4 import MP4

from harmonist import artist_nfo, formats, scanner
from harmonist.formats.m4a import ATOM_ALBUM_ARTISTS, ATOM_MB_ALBUM_ARTIST_ID
from harmonist.web.scan_runner import ScanRunner
from test.test_artist_web import ALICE, BOB, RELEASE, SOLO, make_album
from test.test_nfo_inventory import write_nfo


def picture(folder, name):
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"local-picture")
    return path.resolve()


def selected(root, identity=ALICE):
    inventory = artist_nfo.scan(root)
    profile = inventory.for_artist(identity).profile
    assert profile is not None
    return inventory.image_for(identity)


@pytest.mark.parametrize("reference", ["photos/portrait.png", "../shared/portrait.png", "absolute"])
def test_thumb_wins_over_explicit_and_generic_names_inside_the_library(tmp_path, reference):
    nfo = write_nfo(tmp_path, "Artist", ALICE)
    make_album(tmp_path, "Artist/Album", RELEASE, ["Alice"], [ALICE])
    for name in ("artist.jpg", "thumb.jpg", "folder.jpg"):
        picture(nfo.parent, name)
    if reference == "../shared/portrait.png":
        target = picture(tmp_path, "shared/portrait.png")
    else:
        target = picture(tmp_path if reference == "absolute" else nfo.parent, "photos/portrait.png")
    thumb = str(target) if reference == "absolute" else reference
    nfo.write_text(
        f"<artist><musicbrainzartistid>{ALICE}</musicbrainzartistid><thumb>{thumb}</thumb></artist>"
    )
    assert selected(tmp_path) == target


@pytest.mark.parametrize("thumb", ["", "<thumb>missing.jpg</thumb>"])
def test_explicit_artist_name_precedes_generic_folder_fallback(tmp_path, thumb):
    nfo = write_nfo(tmp_path, "Artist", ALICE)
    make_album(tmp_path, "Artist/Album", RELEASE, ["Alice"], [ALICE])
    nfo.write_text(f"<artist><musicbrainzartistid>{ALICE}</musicbrainzartistid>{thumb}</artist>")
    picture(nfo.parent, "folder.jpg")
    picture(nfo.parent, "thumb.jpg")
    explicit = picture(nfo.parent, "artist.png")
    assert selected(tmp_path) == explicit


@pytest.mark.parametrize("name", ["thumb.jpg", "thumb.png", "folder.jpg", "folder.png"])
def test_generic_picture_needs_artist_folder_evidence(tmp_path, name):
    nfo = write_nfo(tmp_path, "Artist", ALICE)
    image = picture(nfo.parent, name)
    # An NFO and an image alone do not establish what kind of folder this is.
    assert selected(tmp_path) is None
    make_album(tmp_path, "Artist/Album", RELEASE, ["Alice"], [ALICE])
    assert selected(tmp_path) == image
    # The same directory containing another artist's album is not an artist root.
    make_album(tmp_path, "Artist/Other album", SOLO, ["Bob"], [BOB])
    assert selected(tmp_path) is None


@pytest.mark.parametrize("marker", ["album.nfo", ".harmonist.json"])
def test_album_container_with_disc_folders_cannot_supply_an_implicit_picture(tmp_path, marker):
    nfo = write_nfo(tmp_path, "Album", ALICE)
    make_album(tmp_path, "Album/Disc 1", RELEASE, ["Alice"], [ALICE])
    (nfo.parent / marker).write_text("{}" if marker.endswith("json") else "<album/>")
    image = picture(nfo.parent, "folder.jpg")
    assert selected(tmp_path) is None
    # A declared thumb is authoritative even when it explicitly names a cover.
    nfo.write_text(
        f"<artist><musicbrainzartistid>{ALICE}</musicbrainzartistid><thumb>folder.jpg</thumb></artist>"
    )
    assert selected(tmp_path) == image


def test_disc_tags_exclude_an_album_container_without_metadata_markers(tmp_path):
    nfo = write_nfo(tmp_path, "Album", ALICE)
    folder = make_album(tmp_path, "Album/Disc 1", RELEASE, ["Alice"], [ALICE])
    track = next(folder.glob("*.m4a"))
    audio = MP4(track)
    audio["disk"] = [(1, 2)]
    audio.save()
    picture(nfo.parent, "folder.jpg")
    assert selected(tmp_path) is None


def test_profile_picture_updates_without_changing_the_cached_nfo(tmp_path, monkeypatch):
    nfo = write_nfo(tmp_path, "metadata", ALICE)
    runner = ScanRunner(tmp_path)
    runner.refresh_now()
    before = nfo.read_bytes(), nfo.stat().st_mtime_ns
    assert runner.nfo_inventory().for_artist(ALICE).profile.image_path is None
    image = picture(nfo.parent, "artist.png")
    no_parse = Mock(side_effect=AssertionError("unchanged XML must not be parsed again"))
    monkeypatch.setattr(artist_nfo.ET, "fromstring", no_parse)
    runner._refresh_changed({image})
    assert runner.nfo_inventory().for_artist(ALICE).profile.image_path == image
    seq = runner.status()["seq"]
    runner._refresh_changed({image})
    assert runner.status()["seq"] == seq
    image.unlink()
    runner._refresh_changed({image})
    assert runner.nfo_inventory().for_artist(ALICE).profile.image_path is None
    assert (nfo.read_bytes(), nfo.stat().st_mtime_ns) == before
    no_parse.assert_not_called()


def test_thumb_cannot_escape_the_library_through_an_absolute_path_or_symlink(tmp_path):
    root = tmp_path / "music"
    nfo = write_nfo(root, "Artist", ALICE)
    outside = picture(tmp_path, "private.jpg")
    link = nfo.parent / "linked.jpg"
    link.symlink_to(outside)
    for reference in (
        str(outside),
        "linked.jpg",
        "../../private.jpg",
        "https://example.org/photo.jpg",
    ):
        nfo.write_text(
            f"<artist><musicbrainzartistid>{ALICE}</musicbrainzartistid><thumb>{reference}</thumb></artist>"
        )
        assert selected(root) is None


def test_picture_refresh_cannot_replace_a_newer_scan_with_old_folder_evidence(
    tmp_path, monkeypatch
):
    nfo = write_nfo(tmp_path, "Artist", ALICE)
    make_album(tmp_path, "Artist/Album", RELEASE, ["Alice"], [ALICE])
    image = picture(nfo.parent, "folder.jpg")
    runner = ScanRunner(tmp_path)
    runner.refresh_now()
    assert runner.nfo_inventory().image_for(ALICE) == image
    original = artist_nfo.Discovery.finish
    interleaved = False

    def finish(discovery, albums=()):
        nonlocal interleaved
        result = original(discovery, albums)
        if not interleaved:
            interleaved = True
            # A full scan publishes new evidence while the earlier image
            # refresh still holds its old result, as two worker threads can do.
            make_album(tmp_path, "Artist/Other album", SOLO, ["Bob"], [BOB])
            runner.refresh_now()
        return result

    monkeypatch.setattr(artist_nfo.Discovery, "finish", finish)
    runner._refresh_nfos(set())
    assert runner.nfo_inventory().image_for(ALICE) is None


def test_read_error_is_reported_instead_of_falling_back_to_another_picture(tmp_path, monkeypatch):
    nfo = write_nfo(tmp_path, "Artist", ALICE)
    preferred = picture(nfo.parent, "preferred.png")
    picture(nfo.parent, "artist.png")
    nfo.write_text(
        f"<artist><musicbrainzartistid>{ALICE}</musicbrainzartistid><thumb>preferred.png</thumb></artist>"
    )
    original = artist_nfo._present

    def present(path):
        if path == preferred:
            raise PermissionError("picture is unreadable")
        return original(path)

    monkeypatch.setattr(artist_nfo, "_present", present)
    inventory = artist_nfo.scan(tmp_path)
    assert inventory.for_artist(ALICE).profile.image_path is None
    assert any("picture is unreadable" in error for error in inventory.errors)


def test_conflicting_nfos_keep_their_paths_but_cannot_choose_an_artist_picture(tmp_path):
    for name in ("one", "two"):
        nfo = write_nfo(tmp_path, name, ALICE)
        picture(nfo.parent, "artist.png")
    inventory = artist_nfo.scan(tmp_path)
    assert len(inventory.profiles) == 2
    assert all(p.image_path is not None for p in inventory.profiles)
    assert inventory.for_artist(ALICE).profile is None
    assert inventory.image_for(ALICE) is None


def test_retagging_updates_folder_picture_evidence_without_a_full_walk(tmp_path, monkeypatch):
    nfo = write_nfo(tmp_path, "Artist", ALICE)
    folder = make_album(tmp_path, "Artist/Album", RELEASE, ["Alice"], [ALICE])
    image = picture(nfo.parent, "folder.jpg")
    runner = ScanRunner(tmp_path)
    runner.refresh_now()
    assert runner.nfo_inventory().image_for(ALICE) == image
    track = next(folder.glob("*.m4a"))
    audio = MP4(track)
    audio[ATOM_MB_ALBUM_ARTIST_ID] = [BOB.encode()]
    audio[ATOM_ALBUM_ARTISTS] = [b"Bob"]
    audio["aART"] = ["Bob"]
    audio.save()
    no_walk = Mock(side_effect=AssertionError("a tag edit needs no full walk"))
    reads = Mock(wraps=formats.read_scan_fields)
    monkeypatch.setattr(scanner, "iter_album_dirs", no_walk)
    monkeypatch.setattr(formats, "read_scan_fields", reads)
    assert runner._refresh_changed({track})
    assert runner.nfo_inventory().image_for(ALICE) is None
    assert reads.call_count == 1


def test_picture_refresh_reuses_the_already_scanned_album_directories(tmp_path, monkeypatch):
    nfo = write_nfo(tmp_path, "Artist", ALICE)
    folder = make_album(tmp_path, "Artist/Album", RELEASE, ["Alice"], [ALICE])
    image = picture(nfo.parent, "folder.jpg")
    runner = ScanRunner(tmp_path)
    runner.refresh_now()
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        assert path != folder, "a picture refresh must not resolve every audio directory again"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    runner._refresh_nfos(set())
    assert runner.nfo_inventory().image_for(ALICE) == image
