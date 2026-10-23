"""Read existing artist NFOs without directory/name-based identity guesses."""

import os
from dataclasses import replace

import pytest

from harmonist import artist_nfo
from harmonist.artists import Artist
from harmonist.models import Album, AlbumState

ALICE = "11111111-1111-4111-8111-111111111111"
BOB = "22222222-2222-4222-8222-222222222222"


@pytest.mark.parametrize("filename", ["artist.jpg", "artist.png"])
def test_artist_picture_without_a_thumb_reference(tmp_path, filename):
    path = tmp_path / "artist.nfo"
    path.write_text(f"<artist><musicbrainzartistid>{ALICE}</musicbrainzartistid></artist>")
    image = tmp_path / filename
    image.write_bytes(b"local-artist-image")
    assert artist_nfo.local_image(artist_nfo.read(path)) == image


@pytest.mark.parametrize("id_tag", ["musicBrainzArtistID", "musicbrainzartistid"])
def test_parser_aliases_encoding_and_safe_local_art(tmp_path, id_tag):
    path = tmp_path / "artist.nfo"
    path.write_bytes(
        (
            f'<?xml version="1.0" encoding="iso-8859-1"?>'
            f"<artist><name>André</name><{id_tag}>{ALICE}</{id_tag}>"
            "<biography>&lt;script&gt;no&lt;/script&gt;</biography><genre>Jazz</genre>"
            "<genre>Soul</genre><thumb>folder.jpg</thumb></artist>"
        ).encode("latin1")
    )
    image = tmp_path / "folder.jpg"
    image.write_bytes(b"local-image")
    profile = artist_nfo.read(path)
    assert (profile.name, profile.mbid) == ("André", ALICE)
    assert profile.biography == "<script>no</script>"
    assert ("Genre", "Jazz · Soul") in profile.facts
    assert artist_nfo.local_image(profile) == image
    # The same metadata file changed externally: the cached parse must change.
    path.write_text(
        f"<artist><musicbrainzartistid>{ALICE}</musicbrainzartistid>"
        "<name>New name</name><thumb>https://example.org/photo.jpg</thumb></artist>"
    )
    image.unlink()
    assert artist_nfo.read(path).name == "New name"
    assert artist_nfo.local_image(artist_nfo.read(path)) is None


@pytest.mark.parametrize(
    "payload",
    [
        b"<artist>",
        b"<album/>",
        b'<!DOCTYPE artist [<!ENTITY x SYSTEM "file:///etc/passwd">]><artist>&x;</artist>',
        "<!DOCTYPE artist><artist/>".encode("utf-16"),
        b"<artist>" + b"x" * (1024 * 1024) + b"</artist>",
    ],
)
def test_invalid_xml_is_an_error_not_missing_metadata(tmp_path, payload):
    path = tmp_path / "artist.nfo"
    path.write_bytes(payload)
    with pytest.raises(artist_nfo.NfoError):
        artist_nfo.read(path)


def test_information_folder_supports_collaboration_and_rejects_ambiguity(tmp_path):
    music = tmp_path / "music"
    folder = music / "arbitrary" / "layout" / "release"
    folder.mkdir(parents=True)
    album = Album(
        id="together",
        path=folder,
        title="Together",
        artist="Alice & Bob",
        track_count=1,
        state=AlbumState.COMPLETE,
        paths=(folder,),
    )
    alice = Artist(ALICE, "Same name", ALICE, None, (album,))
    info = tmp_path / "ArtistInfo"
    for name, mbid in [("one", ALICE), ("two", BOB)]:
        d = info / name
        d.mkdir(parents=True)
        (d / "artist.nfo").write_text(
            f"<artist><name>Same name</name><musicBrainzArtistID>{mbid}</musicBrainzArtistID></artist>"
        )
    original = (info / "one" / "artist.nfo").read_bytes()
    assert artist_nfo.for_artist(alice, music, info).profile.path == info / "one" / "artist.nfo"
    bob = replace(alice, key=BOB, mbid=BOB)
    assert artist_nfo.for_artist(bob, music, info).profile.path == info / "two" / "artist.nfo"
    # A matching display name without a confirmed ID does not establish identity.
    assert artist_nfo.for_artist(replace(alice, mbid=None), music, info).profile is None
    local = music / "arbitrary" / "artist.nfo"
    local.write_bytes(original)
    ambiguous = artist_nfo.for_artist(alice, music, info)
    assert ambiguous.profile is None
    assert ambiguous.errors
    assert set(ambiguous.candidates) == {local, info / "one" / "artist.nfo"}
    selected = artist_nfo.for_artist(alice, music, info, artist_nfo.selection_key(local))
    assert selected.profile is not None
    assert selected.profile.path == local
    local.unlink()
    assert artist_nfo.for_artist(alice, music, info).profile is not None
    assert (info / "one" / "artist.nfo").read_bytes() == original


@pytest.mark.parametrize("kind", ["relative", "absolute", "symlink", "remote"])
def test_image_references_do_not_escape_nfo_directory(tmp_path, kind):
    directory = tmp_path / "artist"
    directory.mkdir()
    outside = tmp_path / "private.jpg"
    outside.write_bytes(b"private-image")
    (directory / "linked.jpg").symlink_to(outside)
    reference = {
        "relative": "../private.jpg",
        "absolute": str(outside),
        "symlink": "linked.jpg",
        "remote": "https://example.org/a.jpg",
    }[kind]
    path = directory / "artist.nfo"
    path.write_text(f"<artist><thumb>{reference}</thumb></artist>")
    assert artist_nfo.local_image(artist_nfo.read(path)) is None


def test_read_again_bypasses_an_unchanged_file_timestamp(tmp_path):
    info = tmp_path / "info"
    info.mkdir()
    path = info / "artist.nfo"
    template = (
        f"<artist><musicbrainzartistid>{ALICE}</musicbrainzartistid><name>{{}}</name></artist>"
    )
    path.write_text(template.format("Alice"))
    artist = Artist(ALICE, "Alice", ALICE, None, ())
    original = path.stat()
    assert artist_nfo.for_artist(artist, tmp_path, info).profile.name == "Alice"
    path.write_text(template.format("Other"))
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert artist_nfo.for_artist(artist, tmp_path, info, refresh=True).profile.name == "Other"


def test_unreadable_and_missing_information_folders_are_visible_errors(tmp_path):
    artist = Artist(ALICE, "Alice", ALICE, None, ())
    result = artist_nfo.for_artist(artist, tmp_path, tmp_path / "missing")
    assert result.profile is None
    assert result.errors
