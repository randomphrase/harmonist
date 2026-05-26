"""Tests for the Picard-compatible tagger."""

from __future__ import annotations

import pytest
from mutagen.mp4 import MP4

from harmonist import tagger
from harmonist.tagger import (
    ATOM_ALBUM,
    ATOM_ALBUM_ARTIST,
    ATOM_ARTIST,
    ATOM_ASIN,
    ATOM_BARCODE,
    ATOM_CATALOG,
    ATOM_COMMENT,
    ATOM_COVER,
    ATOM_DATE,
    ATOM_DISC_NUM,
    ATOM_LABEL,
    ATOM_MB_ALBUM_ARTIST_ID,
    ATOM_MB_ALBUM_COUNTRY,
    ATOM_MB_ALBUM_ID,
    ATOM_MB_ALBUM_STATUS,
    ATOM_MB_ALBUM_TYPE,
    ATOM_MB_ARTIST_ID,
    ATOM_MB_RELEASE_GROUP_ID,
    ATOM_MB_RELEASE_TRACK_ID,
    ATOM_MB_TRACK_ID,
    ATOM_MEDIA,
    ATOM_TITLE,
    ATOM_TRACK_NUM,
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
                            {
                                "artist": {"id": "art-bbb", "name": "Featured Artist"},
                                "name": "Featured Artist",
                                "joinphrase": " feat. ",
                            },
                            {"artist": {"id": "art-ccc", "name": "Other"}, "name": "Other"},
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
    assert _atom_str(track1, ATOM_MB_ALBUM_STATUS) == "official"  # Picard lower-cases status
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
    assert track1[ATOM_TITLE] == ["Track 1"]
    assert track1[ATOM_ALBUM] == ["Test Album"]
    assert track1[ATOM_ARTIST] == ["Test Artist"]
    assert track1[ATOM_ALBUM_ARTIST] == ["Test Artist"]
    assert track1[ATOM_DATE] == ["2021-06-15"]
    assert track1[ATOM_TRACK_NUM] == [(1, 2)]
    assert track1[ATOM_DISC_NUM] == [(1, 1)]


def test_tag_album_track_artist_credit_overrides_release(album_with_tracks):
    """Track 2 has its own artist-credit; should be used for ©ART and Artist Id atom."""
    album_dir = album_with_tracks(2)
    tagger.tag_album(album_dir, _release_2_tracks())

    track2 = MP4(album_dir / "02 Track 2.m4a")
    assert track2[ATOM_ARTIST] == ["Featured Artist feat. Other"]
    assert _atom_strs(track2, ATOM_MB_ARTIST_ID) == ["art-bbb", "art-ccc"]
    # Album-artist remains the release's primary
    assert track2[ATOM_ALBUM_ARTIST] == ["Test Artist"]


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
    audio[ATOM_COMMENT] = ["https://myartist.bandcamp.com/album/y"]
    audio.save()

    tagger.tag_album(album_dir, _single_track_release())
    audio2 = MP4(f)
    assert audio2[ATOM_COMMENT] == ["https://myartist.bandcamp.com/album/y"]


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
    assert ATOM_COVER in audio
    assert len(audio[ATOM_COVER]) == 1
    assert bytes(audio[ATOM_COVER][0]) == _minimal_jpeg()


def test_tag_album_no_cover_when_path_none(album_with_tracks):
    album_dir = album_with_tracks(1)
    tagger.tag_album(album_dir, _single_track_release(), cover_path=None)
    audio = MP4(album_dir / "01 Track 1.m4a")
    assert ATOM_COVER not in audio


def test_tag_album_returns_count(album_with_tracks):
    album_dir = album_with_tracks(2)
    n = tagger.tag_album(album_dir, _release_2_tracks())
    assert n == 2


# ---------- incomplete-mode tagging (§15.3) ----------


def test_tag_album_incomplete_allows_fewer_files_than_tracks(album_with_tracks):
    """incomplete=True bypasses the count-mismatch raise."""
    album_dir = album_with_tracks(1)  # 1 file
    n = tagger.tag_album(album_dir, _release_2_tracks(), incomplete=True)
    assert n == 1
    audio = MP4(album_dir / "01 Track 1.m4a")
    # The single on-disk file should be tagged with the album MBID
    assert _atom_str(audio, ATOM_MB_ALBUM_ID) == "rel-aaa"


def test_tag_album_incomplete_still_raises_when_too_many_files(album_with_tracks):
    """file_count > track_count is out of scope (per §15.3) — still raises
    even in incomplete mode.
    """
    album_dir = album_with_tracks(3)  # 3 files, release has 1 track
    with pytest.raises(TagMismatchError, match="exceeds"):
        tagger.tag_album(album_dir, _single_track_release(), incomplete=True)


def test_tag_album_incomplete_uses_positional_fallback_without_lengths(
    album_with_tracks,
):
    """With no track lengths in the MB release, incomplete-mode assigns
    files positionally — file 0 → MB track 0.
    """
    album_dir = album_with_tracks(1)  # one file
    tagger.tag_album(album_dir, _release_2_tracks(), incomplete=True)
    audio = MP4(album_dir / "01 Track 1.m4a")
    # MB track 0 ("Track 1") should be the assignment
    assert _atom_str(audio, ATOM_MB_TRACK_ID) == "rec-001"


def test_tag_album_incomplete_uses_length_similarity_when_available(
    album_with_tracks,
):
    """The sine.m4a fixture is ~1000ms long. If MB track lengths differ
    sharply, the assignment should pick the closest match — not positional.
    """
    album_dir = album_with_tracks(1)
    release = _release_2_tracks()
    # Track 0 wildly mismatched (10s); track 1 matches (1s) — incomplete-mode
    # should pick track 1 even though positional would have picked track 0.
    release["medium-list"][0]["track-list"][0]["recording"]["length"] = "10000"
    release["medium-list"][0]["track-list"][1]["recording"]["length"] = "1000"
    tagger.tag_album(album_dir, release, incomplete=True)
    audio = MP4(album_dir / "01 Track 1.m4a")
    assert _atom_str(audio, ATOM_MB_TRACK_ID) == "rec-002"


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
