"""Tests for the Picard-compatible tagger."""
from __future__ import annotations

from pathlib import Path

import pytest
from mutagen.mp4 import MP4

from harmonist import tagger
from harmonist.tagger import (
    ATOM_LABEL,
    ATOM_CATALOG,
    ATOM_BARCODE,
    ATOM_ASIN,
    ATOM_MEDIA,
    ATOM_MB_ALBUM_ARTIST_ID,
    ATOM_MB_ALBUM_COUNTRY,
    ATOM_MB_ALBUM_ID,
    ATOM_MB_ALBUM_STATUS,
    ATOM_MB_ALBUM_TYPE,
    ATOM_MB_ARTIST_ID,
    ATOM_MB_RELEASE_GROUP_ID,
    ATOM_MB_RELEASE_TRACK_ID,
    ATOM_MB_TRACK_ID,
    LEGACY_RELEASE_ID,
    TagMismatchError,
)


def _release_2_tracks() -> dict:
    """Build a synthetic 2-track MB release dict with all the trimmings."""
    return {
        "id": "rel-aaa",
        "title": "Test Album",
        "status": "Official",
        "country": "GB",
        "date": "2021-06-15",
        "barcode": "0123456789012",
        "asin": "B00ASIN1234",
        "artist-credit": [
            {"artist": {"id": "art-aaa", "name": "Test Artist"}, "name": "Test Artist"},
        ],
        "release-group": {"id": "rg-aaa", "primary-type": "Album"},
        "label-info-list": [
            {"label": {"name": "Test Label"}, "catalog-number": "CAT-001"},
        ],
        "medium-list": [
            {
                "position": "1",
                "format": "Digital Media",
                "track-list": [
                    {
                        "id": "rt-001",
                        "position": "1",
                        "title": "Track 1",
                        "recording": {"id": "rec-001", "title": "Track 1"},
                    },
                    {
                        "id": "rt-002",
                        "position": "2",
                        "title": "Track 2",
                        "recording": {"id": "rec-002", "title": "Track 2"},
                        "artist-credit": [
                            {"artist": {"id": "art-bbb", "name": "Featured Artist"},
                             "name": "Featured Artist", "joinphrase": " feat. "},
                            {"artist": {"id": "art-ccc", "name": "Other"},
                             "name": "Other"},
                        ],
                    },
                ],
            }
        ],
    }


def _atom_str(audio: MP4, atom: str) -> str:
    return audio[atom][0].decode("utf-8")


def _atom_strs(audio: MP4, atom: str) -> list[str]:
    return [v.decode("utf-8") for v in audio[atom]]


def test_tag_album_writes_all_album_atoms(album_with_tracks):
    album_dir = album_with_tracks(2)
    tagger.tag_album(album_dir, _release_2_tracks())

    track1 = MP4(album_dir / "01 Track 1.m4a")
    assert _atom_str(track1, ATOM_MB_ALBUM_ID) == "rel-aaa"
    assert _atom_strs(track1, ATOM_MB_ALBUM_ARTIST_ID) == ["art-aaa"]
    assert _atom_str(track1, ATOM_MB_RELEASE_GROUP_ID) == "rg-aaa"
    assert _atom_str(track1, ATOM_MB_ALBUM_TYPE) == "Album"
    assert _atom_str(track1, ATOM_MB_ALBUM_STATUS) == "Official"
    assert _atom_str(track1, ATOM_MB_ALBUM_COUNTRY) == "GB"
    assert _atom_str(track1, ATOM_LABEL) == "Test Label"
    assert _atom_str(track1, ATOM_CATALOG) == "CAT-001"
    assert _atom_str(track1, ATOM_BARCODE) == "0123456789012"
    assert _atom_str(track1, ATOM_ASIN) == "B00ASIN1234"
    assert _atom_str(track1, ATOM_MEDIA) == "Digital Media"


def test_tag_album_writes_per_track_atoms(album_with_tracks):
    album_dir = album_with_tracks(2)
    tagger.tag_album(album_dir, _release_2_tracks())

    track1 = MP4(album_dir / "01 Track 1.m4a")
    track2 = MP4(album_dir / "02 Track 2.m4a")

    assert _atom_str(track1, ATOM_MB_TRACK_ID) == "rec-001"
    assert _atom_str(track1, ATOM_MB_RELEASE_TRACK_ID) == "rt-001"
    assert _atom_str(track2, ATOM_MB_TRACK_ID) == "rec-002"
    assert _atom_str(track2, ATOM_MB_RELEASE_TRACK_ID) == "rt-002"


def test_tag_album_writes_standard_text_tags(album_with_tracks):
    album_dir = album_with_tracks(2)
    tagger.tag_album(album_dir, _release_2_tracks())

    track1 = MP4(album_dir / "01 Track 1.m4a")
    assert track1["\xa9nam"] == ["Track 1"]
    assert track1["\xa9alb"] == ["Test Album"]
    assert track1["\xa9ART"] == ["Test Artist"]
    assert track1["aART"] == ["Test Artist"]
    assert track1["\xa9day"] == ["2021-06-15"]
    assert track1["trkn"] == [(1, 2)]
    assert track1["disk"] == [(1, 1)]


def test_tag_album_track_artist_credit_overrides_release(album_with_tracks):
    """Track 2 has its own artist-credit; should be used for ©ART and Artist Id atom."""
    album_dir = album_with_tracks(2)
    tagger.tag_album(album_dir, _release_2_tracks())

    track2 = MP4(album_dir / "02 Track 2.m4a")
    assert track2["\xa9ART"] == ["Featured Artist feat. Other"]
    assert _atom_strs(track2, ATOM_MB_ARTIST_ID) == ["art-bbb", "art-ccc"]
    # Album-artist remains the release's primary
    assert track2["aART"] == ["Test Artist"]


def test_tag_album_removes_legacy_release_id(album_with_tracks):
    album_dir = album_with_tracks(1)
    f = album_dir / "01 Track 1.m4a"
    audio = MP4(f)
    audio[LEGACY_RELEASE_ID] = [b"old-broken-mbid"]
    audio.save()

    tagger.tag_album(album_dir, _single_track_release())
    audio2 = MP4(f)
    assert LEGACY_RELEASE_ID not in audio2


def test_tag_album_preserves_comment_atom(album_with_tracks):
    album_dir = album_with_tracks(1)
    f = album_dir / "01 Track 1.m4a"
    audio = MP4(f)
    audio["\xa9cmt"] = ["https://myartist.bandcamp.com/album/y"]
    audio.save()

    tagger.tag_album(album_dir, _single_track_release())
    audio2 = MP4(f)
    assert audio2["\xa9cmt"] == ["https://myartist.bandcamp.com/album/y"]


def test_tag_album_idempotent(album_with_tracks):
    """Running twice produces the same atom values — no duplication, no error."""
    album_dir = album_with_tracks(1)
    rel = _single_track_release()

    tagger.tag_album(album_dir, rel)
    audio_first = dict(MP4(album_dir / "01 Track 1.m4a"))
    tagger.tag_album(album_dir, rel)
    audio_second = dict(MP4(album_dir / "01 Track 1.m4a"))

    assert audio_first == audio_second


def test_tag_album_count_mismatch_raises(album_with_tracks):
    album_dir = album_with_tracks(3)  # 3 files
    with pytest.raises(TagMismatchError):
        tagger.tag_album(album_dir, _single_track_release())


def test_tag_album_embeds_cover_art(album_with_tracks, tmp_path):
    album_dir = album_with_tracks(1)
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(_minimal_jpeg())

    tagger.tag_album(album_dir, _single_track_release(), cover_path=cover)
    audio = MP4(album_dir / "01 Track 1.m4a")
    assert "covr" in audio
    assert len(audio["covr"]) == 1
    assert bytes(audio["covr"][0]) == _minimal_jpeg()


def test_tag_album_no_cover_when_path_none(album_with_tracks):
    album_dir = album_with_tracks(1)
    tagger.tag_album(album_dir, _single_track_release(), cover_path=None)
    audio = MP4(album_dir / "01 Track 1.m4a")
    assert "covr" not in audio


def test_tag_album_returns_count(album_with_tracks):
    album_dir = album_with_tracks(2)
    n = tagger.tag_album(album_dir, _release_2_tracks())
    assert n == 2


# -- helpers used in multiple tests --

def _single_track_release() -> dict:
    return {
        "id": "rel-aaa",
        "title": "Test Album",
        "status": "Official",
        "artist-credit": [
            {"artist": {"id": "art-aaa", "name": "Test Artist"}, "name": "Test Artist"},
        ],
        "release-group": {"id": "rg-aaa", "primary-type": "Album"},
        "medium-list": [
            {
                "position": "1",
                "track-list": [
                    {
                        "id": "rt-001",
                        "position": "1",
                        "title": "Track 1",
                        "recording": {"id": "rec-001", "title": "Track 1"},
                    },
                ],
            }
        ],
    }


def _minimal_jpeg() -> bytes:
    """Arbitrary bytes with a JPEG magic header — mutagen doesn't validate."""
    return b"\xff\xd8\xff\xe0" + b"FAKE_JPEG_BODY" * 8 + b"\xff\xd9"
