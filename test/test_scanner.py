"""Tests for the sidecar-driven scanner."""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from harmonist import sidecar as sc, tagger
from harmonist.models import (
    Album,
    AlbumState,
    BandcampInfo,
    MatchCandidate,
    Sidecar,
)
from harmonist.scanner import scan
from harmonist.sidecar import CURRENT_SCHEMA_VERSION


FIXTURES_DIR = Path(__file__).parent / "fixtures"
SINE_M4A = FIXTURES_DIR / "sine.m4a"


def _make_album_dir(root: Path, artist: str, album: str, n_tracks: int = 1) -> Path:
    d = root / artist / album
    d.mkdir(parents=True)
    for i in range(1, n_tracks + 1):
        shutil.copy(SINE_M4A, d / f"{i:02d} Track {i}.m4a")
    return d


def test_scan_empty_dir_returns_nothing(tmp_path):
    assert scan(tmp_path) == []


def test_scan_missing_dir_returns_nothing(tmp_path):
    assert scan(tmp_path / "nope") == []


def test_scan_new_when_no_sidecar(tmp_path):
    album_dir = _make_album_dir(tmp_path, "Artist", "Album")
    albums = scan(tmp_path)
    assert len(albums) == 1
    a = albums[0]
    assert a.state == AlbumState.NEW
    assert a.path == album_dir
    assert a.track_count == 1
    assert a.sidecar is None
    # NEW album gets a registry-minted UUID (32 hex chars)
    assert len(a.id) == 32
    # Same album → same id on repeat scan (registry preserves)
    assert scan(tmp_path)[0].id == a.id


def test_scan_needs_mbid_when_sidecar_has_store_url(tmp_path):
    album_dir = _make_album_dir(tmp_path, "Artist", "Album")
    sc.write(
        album_dir,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=1),
            downloaded_at=datetime.now(timezone.utc),
        ),
    )
    a = scan(tmp_path)[0]
    assert a.state == AlbumState.NEEDS_MBID
    assert a.sidecar.store_url == "https://x.bandcamp.com/album/y"


def test_scan_needs_mbid_when_sidecar_has_no_store_url(tmp_path):
    album_dir = _make_album_dir(tmp_path, "Artist", "Album")
    sc.write(
        album_dir,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            added_at=datetime.now(timezone.utc),
        ),
    )
    a = scan(tmp_path)[0]
    assert a.state == AlbumState.NEEDS_MBID


def test_scan_needs_review_when_match_candidate_set(tmp_path):
    album_dir = _make_album_dir(tmp_path, "Artist", "Album")
    sc.write(
        album_dir,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=1),
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-aaa",
                confidence="approximate",
                file_count=1,
                track_count=2,
            ),
        ),
    )
    a = scan(tmp_path)[0]
    assert a.state == AlbumState.NEEDS_REVIEW
    assert a.sidecar.mb_match_candidate.mb_release_id == "rel-aaa"


def test_scan_tagging_when_mbid_set_but_files_not_tagged(tmp_path):
    album_dir = _make_album_dir(tmp_path, "Artist", "Album")
    sc.write(
        album_dir,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            mb_release_id="rel-aaa",
            added_at=datetime.now(timezone.utc),
        ),
    )
    a = scan(tmp_path)[0]
    assert a.state == AlbumState.TAGGING


def test_scan_needs_sync_when_item_id_missing(tmp_path):
    """Tagged album with bandcamp store_url but no item_id → NEEDS_SYNC."""
    album_dir = _make_album_dir(tmp_path, "Artist", "Album")
    release = {
        "id": "rel-aaa",
        "title": "Album",
        "release-group": {"id": "rg-aaa"},
        "medium-list": [
            {"position": "1", "track-list": [
                {"id": "rt-1", "title": "T1", "recording": {"id": "rec-1", "title": "T1"}}
            ]}
        ],
    }
    tagger.tag_album(album_dir, release)
    sc.write(
        album_dir,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=None),
            mb_release_id="rel-aaa",
            tagged_at=datetime.now(timezone.utc),
        ),
    )
    a = scan(tmp_path)[0]
    assert a.state == AlbumState.NEEDS_SYNC


def test_scan_done_when_bandcamp_item_id_present(tmp_path):
    """Tagged album with bandcamp store_url + item_id → DONE."""
    album_dir = _make_album_dir(tmp_path, "Artist", "Album")
    release = {
        "id": "rel-aaa", "title": "Album",
        "release-group": {"id": "rg-aaa"},
        "medium-list": [{"position": "1", "track-list": [
            {"id": "rt-1", "title": "T1", "recording": {"id": "rec-1", "title": "T1"}}
        ]}],
    }
    tagger.tag_album(album_dir, release)
    sc.write(
        album_dir,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=12345),
            mb_release_id="rel-aaa",
            tagged_at=datetime.now(timezone.utc),
        ),
    )
    assert scan(tmp_path)[0].state == AlbumState.DONE


def test_scan_done_when_mbid_set_and_files_tagged(tmp_path):
    """End-to-end: tag a file using tagger, then verify scanner reports DONE."""
    album_dir = _make_album_dir(tmp_path, "Artist", "Album")
    release = {
        "id": "rel-aaa",
        "title": "Album",
        "artist-credit": [{"artist": {"id": "a1", "name": "Artist"}, "name": "Artist"}],
        "release-group": {"id": "rg-aaa", "primary-type": "Album"},
        "medium-list": [
            {
                "position": "1",
                "track-list": [
                    {"id": "rt-1", "position": "1", "title": "Track 1",
                     "recording": {"id": "rec-1", "title": "Track 1"}},
                ],
            }
        ],
    }
    tagger.tag_album(album_dir, release)
    sc.write(
        album_dir,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            mb_release_id="rel-aaa",
            tagged_at=datetime.now(timezone.utc),
        ),
    )
    a = scan(tmp_path)[0]
    assert a.state == AlbumState.DONE


def test_scan_done_check_only_matches_correct_mbid(tmp_path):
    """If files are tagged with a DIFFERENT mbid than sidecar claims, state is TAGGING."""
    album_dir = _make_album_dir(tmp_path, "Artist", "Album")
    release = {
        "id": "rel-bbb",  # different from sidecar's mb_release_id
        "title": "Album",
        "release-group": {"id": "rg-bbb"},
        "medium-list": [
            {"position": "1", "track-list": [
                {"id": "rt-1", "title": "T1", "recording": {"id": "rec-1", "title": "T1"}}
            ]}
        ],
    }
    tagger.tag_album(album_dir, release)
    sc.write(
        album_dir,
        Sidecar(schema_version=CURRENT_SCHEMA_VERSION, mb_release_id="rel-aaa"),
    )
    assert scan(tmp_path)[0].state == AlbumState.TAGGING


def test_scan_finds_multiple_albums(tmp_path):
    _make_album_dir(tmp_path, "A1", "Album 1", n_tracks=2)
    _make_album_dir(tmp_path, "A2", "Album 2", n_tracks=3)
    albums = scan(tmp_path)
    assert {a.title for a in albums} == {"Album 1", "Album 2"}
    by_title = {a.title: a for a in albums}
    assert by_title["Album 1"].track_count == 2
    assert by_title["Album 2"].track_count == 3


def test_scan_picks_up_cover_jpg(tmp_path):
    album_dir = _make_album_dir(tmp_path, "Artist", "Album")
    (album_dir / "cover.jpg").write_bytes(b"jpeg")
    a = scan(tmp_path)[0]
    assert a.cover_path == album_dir / "cover.jpg"


def test_scan_picks_up_cover_png(tmp_path):
    album_dir = _make_album_dir(tmp_path, "Artist", "Album")
    (album_dir / "cover.png").write_bytes(b"png")
    a = scan(tmp_path)[0]
    assert a.cover_path == album_dir / "cover.png"


def test_scan_no_cover(tmp_path):
    _make_album_dir(tmp_path, "Artist", "Album")
    a = scan(tmp_path)[0]
    assert a.cover_path is None


def test_scan_skips_dirs_without_m4a(tmp_path):
    (tmp_path / "Artist" / "Album").mkdir(parents=True)
    (tmp_path / "Artist" / "Album" / "notes.txt").write_text("not music")
    assert scan(tmp_path) == []


def test_scan_reads_album_and_artist_from_tags(tmp_path):
    album_dir = _make_album_dir(tmp_path, "DiskArtist", "DiskAlbum")
    # Set tags on the file
    from mutagen.mp4 import MP4

    audio = MP4(album_dir / "01 Track 1.m4a")
    audio["\xa9alb"] = ["Tag Album Title"]
    audio["\xa9ART"] = ["Tag Artist"]
    audio.save()

    a = scan(tmp_path)[0]
    assert a.title == "Tag Album Title"
    assert a.artist == "Tag Artist"


def test_scan_falls_back_to_dir_name_when_no_album_tag(tmp_path):
    album_dir = _make_album_dir(tmp_path, "Artist", "FallbackName")
    # Default sine.m4a has no ©alb tag
    a = scan(tmp_path)[0]
    assert a.title == "FallbackName"


def test_scan_skips_album_with_invalid_sidecar(tmp_path, caplog):
    """A malformed sidecar should be logged and skipped, not crash the scan."""
    good_dir = _make_album_dir(tmp_path, "Good", "Album")
    bad_dir = _make_album_dir(tmp_path, "Bad", "Album")
    sc.sidecar_path(bad_dir).write_text(
        '{"schema_version": 99}', encoding="utf-8"
    )
    albums = scan(tmp_path)
    paths = {a.path for a in albums}
    assert good_dir in paths
    assert bad_dir not in paths
