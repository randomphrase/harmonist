"""Tests for the sidecar-driven scanner."""

from __future__ import annotations

import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

from harmonist import sidecar as sc
from harmonist import tagger
from harmonist.models import (
    AlbumState,
    BandcampInfo,
    MatchCandidate,
    Sidecar,
)
from harmonist.scanner import scan
from harmonist.sidecar import CURRENT_SCHEMA_VERSION
from harmonist.tagger import (
    ATOM_ALBUM,
    ATOM_ARTIST,
    ATOM_MB_ALBUM_ID,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SINE_M4A = FIXTURES_DIR / "sine.m4a"


def _make_album_dir(root: Path, artist: str, album: str, n_tracks: int = 1) -> Path:
    d = root / artist / album
    d.mkdir(parents=True)
    for i in range(1, n_tracks + 1):
        shutil.copy(SINE_M4A, d / f"{i:02d} Track {i}.m4a")
    return d


def _tag_tracks(
    album_dir: Path, *, album: str, artists: list[str], album_artist: str | None
) -> None:
    """Set per-track artist (and optional album-artist) atoms on an album's files,
    one artist per track in order."""
    from mutagen.mp4 import MP4

    files = sorted(album_dir.glob("*.m4a"))
    for f, art in zip(files, artists, strict=True):
        audio = MP4(f)
        audio["\xa9alb"] = [album]
        audio["\xa9ART"] = [art]
        if album_artist is not None:
            audio["aART"] = [album_artist]
        audio.save()


def test_scan_empty_dir_returns_nothing(tmp_path):
    assert scan(tmp_path) == []


def test_compilation_without_album_artist_shows_various_artists(tmp_path):
    """A compilation (tracks disagree on artist, no album-artist tag) displays
    'Various Artists', not the first track's artist."""
    d = _make_album_dir(tmp_path, "Comps", "Mixtape", n_tracks=3)
    _tag_tracks(d, album="Mixtape", artists=["Alice", "Bob", "Carol"], album_artist=None)
    a = scan(tmp_path)[0]
    assert a.artist == "Various Artists"


def test_album_artist_tag_is_authoritative(tmp_path):
    """When present, the album-artist tag wins (a Picard-tagged compilation carries
    'Various Artists' there even though track artists vary)."""
    d = _make_album_dir(tmp_path, "Comps", "Curated", n_tracks=2)
    _tag_tracks(d, album="Curated", artists=["Alice", "Bob"], album_artist="Various Artists")
    assert scan(tmp_path)[0].artist == "Various Artists"


def test_single_artist_album_unaffected(tmp_path):
    """A normal album (consistent track artist, no album-artist tag) still shows
    that artist — the compilation handling doesn't regress the common case."""
    d = _make_album_dir(tmp_path, "Solo", "Record", n_tracks=2)
    _tag_tracks(d, album="Record", artists=["Solo Act", "Solo Act"], album_artist=None)
    assert scan(tmp_path)[0].artist == "Solo Act"


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
            downloaded_at=datetime.now(UTC),
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
            added_at=datetime.now(UTC),
        ),
    )
    a = scan(tmp_path)[0]
    assert a.state == AlbumState.NEEDS_MBID


def test_scan_needs_mbid_when_match_candidate_set(tmp_path):
    # A pending suggestion (mb_match_candidate) no longer has its own state;
    # it's NEEDS_MBID with the candidate attached — the card adapts.
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
    assert a.state == AlbumState.NEEDS_MBID
    assert a.sidecar.mb_match_candidate.mb_release_id == "rel-aaa"


def test_scan_tagging_when_mbid_set_but_files_not_tagged(tmp_path):
    album_dir = _make_album_dir(tmp_path, "Artist", "Album")
    sc.write(
        album_dir,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            mb_release_id="rel-aaa",
            added_at=datetime.now(UTC),
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
            {
                "position": "1",
                "track-list": [
                    {"id": "rt-1", "title": "T1", "recording": {"id": "rec-1", "title": "T1"}}
                ],
            }
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
            tagged_at=datetime.now(UTC),
        ),
    )
    a = scan(tmp_path)[0]
    assert a.state == AlbumState.NEEDS_SYNC


def test_scan_ambiguous_link_is_complete_not_needs_sync(tmp_path):
    """An ambiguously-linked album (no single item_id, but candidate_item_ids
    recorded — several editions share a store URL) is as resolved as we can get,
    so it's COMPLETE, not stuck in NEEDS_SYNC."""
    album_dir = _make_album_dir(tmp_path, "Artist", "Album")
    release = {
        "id": "rel-aaa",
        "title": "Album",
        "release-group": {"id": "rg-aaa"},
        "medium-list": [
            {
                "position": "1",
                "track-list": [
                    {"id": "rt-1", "title": "T1", "recording": {"id": "rec-1", "title": "T1"}}
                ],
            }
        ],
    }
    tagger.tag_album(album_dir, release)
    sc.write(
        album_dir,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=None, candidate_item_ids=[111, 222]),
            mb_release_id="rel-aaa",
            tagged_at=datetime.now(UTC),
        ),
    )
    a = scan(tmp_path)[0]
    assert a.state == AlbumState.COMPLETE


def test_scan_done_when_bandcamp_item_id_present(tmp_path):
    """Tagged album with bandcamp store_url + item_id → DONE."""
    album_dir = _make_album_dir(tmp_path, "Artist", "Album")
    release = {
        "id": "rel-aaa",
        "title": "Album",
        "release-group": {"id": "rg-aaa"},
        "medium-list": [
            {
                "position": "1",
                "track-list": [
                    {"id": "rt-1", "title": "T1", "recording": {"id": "rec-1", "title": "T1"}}
                ],
            }
        ],
    }
    tagger.tag_album(album_dir, release)
    sc.write(
        album_dir,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=12345),
            mb_release_id="rel-aaa",
            tagged_at=datetime.now(UTC),
        ),
    )
    assert scan(tmp_path)[0].state == AlbumState.COMPLETE


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
                    {
                        "id": "rt-1",
                        "position": "1",
                        "title": "Track 1",
                        "recording": {"id": "rec-1", "title": "Track 1"},
                    },
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
            tagged_at=datetime.now(UTC),
        ),
    )
    a = scan(tmp_path)[0]
    assert a.state == AlbumState.COMPLETE


def test_scan_complete_when_file_count_matches_expected(tmp_path):
    """track_count_expected == file_count → COMPLETE."""
    album_dir = _make_album_dir(tmp_path, "Artist", "Album", n_tracks=2)
    release = {
        "id": "rel-aaa",
        "title": "Album",
        "release-group": {"id": "rg-aaa"},
        "medium-list": [
            {
                "position": "1",
                "track-list": [
                    {"id": "rt-1", "title": "T1", "recording": {"id": "rec-1", "title": "T1"}},
                    {"id": "rt-2", "title": "T2", "recording": {"id": "rec-2", "title": "T2"}},
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
            tagged_at=datetime.now(UTC),
            track_count_expected=2,
        ),
    )
    assert scan(tmp_path)[0].state == AlbumState.COMPLETE


def test_scan_incomplete_when_file_count_less_than_expected(tmp_path):
    """track_count_expected > file_count → INCOMPLETE."""
    album_dir = _make_album_dir(tmp_path, "Artist", "Album", n_tracks=2)
    release = {
        "id": "rel-aaa",
        "title": "Album",
        "release-group": {"id": "rg-aaa"},
        "medium-list": [
            {
                "position": "1",
                "track-list": [
                    {"id": "rt-1", "title": "T1", "recording": {"id": "rec-1", "title": "T1"}},
                    {"id": "rt-2", "title": "T2", "recording": {"id": "rec-2", "title": "T2"}},
                ],
            }
        ],
    }
    # Tag in incomplete mode (so we don't crash on the count mismatch)
    tagger.tag_album(album_dir, release, incomplete=True) if False else None
    # Actually just write the tags manually for the test
    from mutagen.mp4 import MP4

    for f in sorted(album_dir.glob("*.m4a")):
        audio = MP4(f)
        audio[ATOM_MB_ALBUM_ID] = [b"rel-aaa"]
        audio.save()
    sc.write(
        album_dir,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            mb_release_id="rel-aaa",
            tagged_at=datetime.now(UTC),
            track_count_expected=5,  # MB says 5 tracks; only 2 on disk
        ),
    )
    assert scan(tmp_path)[0].state == AlbumState.INCOMPLETE


def test_scan_complete_without_expected_count_legacy(tmp_path):
    """A sidecar without track_count_expected (legacy / no incomplete-aware
    tag yet) still resolves to COMPLETE — we don't penalise unknown.
    """
    album_dir = _make_album_dir(tmp_path, "Artist", "Album", n_tracks=2)
    from mutagen.mp4 import MP4

    for f in sorted(album_dir.glob("*.m4a")):
        audio = MP4(f)
        audio[ATOM_MB_ALBUM_ID] = [b"rel-aaa"]
        audio.save()
    sc.write(
        album_dir,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            mb_release_id="rel-aaa",
            tagged_at=datetime.now(UTC),
            track_count_expected=None,
        ),
    )
    assert scan(tmp_path)[0].state == AlbumState.COMPLETE


def test_scan_incomplete_promotes_to_complete_on_file_addition(tmp_path):
    """If the user later drops the missing track into the dir, the next
    scan sees file_count == track_count_expected and promotes to COMPLETE.
    No sidecar mutation required — pure scanner derivation.
    """
    album_dir = _make_album_dir(tmp_path, "Artist", "Album", n_tracks=2)
    from mutagen.mp4 import MP4

    for f in sorted(album_dir.glob("*.m4a")):
        audio = MP4(f)
        audio[ATOM_MB_ALBUM_ID] = [b"rel-aaa"]
        audio.save()
    sc.write(
        album_dir,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            mb_release_id="rel-aaa",
            tagged_at=datetime.now(UTC),
            track_count_expected=3,
        ),
    )
    assert scan(tmp_path)[0].state == AlbumState.INCOMPLETE

    # User drops in the third file, tagged with the same MBID
    third = album_dir / "03 Track 3.m4a"
    shutil.copy(SINE_M4A, third)
    audio = MP4(third)
    audio[ATOM_MB_ALBUM_ID] = [b"rel-aaa"]
    audio.save()

    assert scan(tmp_path)[0].state == AlbumState.COMPLETE


def test_scan_done_check_only_matches_correct_mbid(tmp_path):
    """If files are tagged with a DIFFERENT mbid than sidecar claims, state is TAGGING."""
    album_dir = _make_album_dir(tmp_path, "Artist", "Album")
    release = {
        "id": "rel-bbb",  # different from sidecar's mb_release_id
        "title": "Album",
        "release-group": {"id": "rg-bbb"},
        "medium-list": [
            {
                "position": "1",
                "track-list": [
                    {"id": "rt-1", "title": "T1", "recording": {"id": "rec-1", "title": "T1"}}
                ],
            }
        ],
    }
    tagger.tag_album(album_dir, release)
    sc.write(
        album_dir,
        Sidecar(schema_version=CURRENT_SCHEMA_VERSION, mb_release_id="rel-aaa"),
    )
    assert scan(tmp_path)[0].state == AlbumState.TAGGING


# ---------- INCONSISTENT detection (§15.2) ----------


def _tag_file(
    path: Path, *, album: str | None = None, mbid: str | None = None, artist: str | None = None
) -> None:
    from mutagen.mp4 import MP4

    audio = MP4(path)
    if album is not None:
        audio[ATOM_ALBUM] = [album]
    if mbid is not None:
        audio[ATOM_MB_ALBUM_ID] = [mbid.encode("utf-8")]
    if artist is not None:
        audio[ATOM_ARTIST] = [artist]
    audio.save()


def test_scan_inconsistent_when_album_titles_disagree(tmp_path):
    album_dir = _make_album_dir(tmp_path, "Artist", "Mess", n_tracks=3)
    files = sorted(album_dir.glob("*.m4a"))
    _tag_file(files[0], album="Album A")
    _tag_file(files[1], album="Album A")
    _tag_file(files[2], album="Album B")  # the outlier
    a = scan(tmp_path)[0]
    assert a.state == AlbumState.INCONSISTENT
    assert len(a.inconsistent_tracks) == 3
    titles = {t.album_title for t in a.inconsistent_tracks}
    assert titles == {"Album A", "Album B"}


def test_scan_inconsistent_when_mbids_disagree(tmp_path):
    album_dir = _make_album_dir(tmp_path, "Artist", "Mess", n_tracks=2)
    files = sorted(album_dir.glob("*.m4a"))
    _tag_file(files[0], album="Shared Title", mbid="rel-aaa")
    _tag_file(files[1], album="Shared Title", mbid="rel-bbb")
    a = scan(tmp_path)[0]
    assert a.state == AlbumState.INCONSISTENT


def test_scan_compilation_is_not_inconsistent(tmp_path):
    """Same album title + MBID, varying track artists = legit compilation."""
    album_dir = _make_album_dir(tmp_path, "VA", "Mixtape", n_tracks=3)
    files = sorted(album_dir.glob("*.m4a"))
    _tag_file(files[0], album="Mixtape", mbid="rel-aaa", artist="Artist 1")
    _tag_file(files[1], album="Mixtape", mbid="rel-aaa", artist="Artist 2")
    _tag_file(files[2], album="Mixtape", mbid="rel-aaa", artist="Artist 3")
    a = scan(tmp_path)[0]
    assert a.state != AlbumState.INCONSISTENT
    assert a.inconsistent_tracks == []


def test_scan_missing_tags_do_not_vote(tmp_path):
    """A file without ©alb or MBID atom doesn't count as a distinct value
    — partial tagging is a separate concern (§15.1).
    """
    album_dir = _make_album_dir(tmp_path, "Artist", "PartTagged", n_tracks=3)
    files = sorted(album_dir.glob("*.m4a"))
    _tag_file(files[0], album="Album X", mbid="rel-aaa")
    _tag_file(files[1], album="Album X", mbid="rel-aaa")
    # files[2] has no ©alb or MBID — should be treated as missing, not "different"
    a = scan(tmp_path)[0]
    assert a.state != AlbumState.INCONSISTENT


def test_scan_inconsistent_trumps_existing_sidecar(tmp_path):
    """A sidecar pointing at COMPLETE is overridden when files become
    inconsistent (e.g. user dropped a stray file into a Done album dir).
    The sidecar isn't deleted — once the user fixes the on-disk reality,
    state derivation resumes from the sidecar.
    """
    album_dir = _make_album_dir(tmp_path, "Artist", "Was Done", n_tracks=2)
    files = sorted(album_dir.glob("*.m4a"))
    _tag_file(files[0], album="Original", mbid="rel-original")
    _tag_file(files[1], album="Stray", mbid="rel-stray")
    sc.write(
        album_dir,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            mb_release_id="rel-original",
            tagged_at=datetime.now(UTC),
        ),
    )
    a = scan(tmp_path)[0]
    assert a.state == AlbumState.INCONSISTENT
    # Sidecar still present — to be re-used after user resolves on-disk
    assert sc.has_sidecar(album_dir)


def test_scan_single_file_album_cannot_be_inconsistent(tmp_path):
    album_dir = _make_album_dir(tmp_path, "Artist", "Single", n_tracks=1)
    files = sorted(album_dir.glob("*.m4a"))
    _tag_file(files[0], album="Some Album", mbid="rel-x")
    a = scan(tmp_path)[0]
    assert a.state != AlbumState.INCONSISTENT


# ---------- partial tagging detection (§15.1) ----------


def test_partial_tag_count_when_some_files_missing_mbid(tmp_path):
    """N of M files have the MBID atom (0 < N < M) → partial_tag_count
    populated. State remains COMPLETE (any-match logic).
    """
    album_dir = _make_album_dir(tmp_path, "Artist", "Partial", n_tracks=3)
    from mutagen.mp4 import MP4

    files = sorted(album_dir.glob("*.m4a"))
    # Tag two of three files
    for f in files[:2]:
        audio = MP4(f)
        audio[ATOM_MB_ALBUM_ID] = [b"rel-aaa"]
        audio.save()
    sc.write(
        album_dir,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            mb_release_id="rel-aaa",
            tagged_at=datetime.now(UTC),
        ),
    )
    a = scan(tmp_path)[0]
    assert a.state == AlbumState.COMPLETE
    assert a.partial_tag_count == (2, 3)


def test_partial_tag_count_none_when_all_tagged(tmp_path):
    """All files tagged → partial_tag_count is None."""
    album_dir = _make_album_dir(tmp_path, "Artist", "Whole", n_tracks=2)
    from mutagen.mp4 import MP4

    for f in sorted(album_dir.glob("*.m4a")):
        audio = MP4(f)
        audio[ATOM_MB_ALBUM_ID] = [b"rel-aaa"]
        audio.save()
    sc.write(
        album_dir,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            mb_release_id="rel-aaa",
            tagged_at=datetime.now(UTC),
        ),
    )
    a = scan(tmp_path)[0]
    assert a.partial_tag_count is None


def test_partial_tag_count_none_without_mbid(tmp_path):
    """No mb_release_id on sidecar → can't compute partial tagging."""
    album_dir = _make_album_dir(tmp_path, "Artist", "NoMBID", n_tracks=2)
    sc.write(album_dir, Sidecar(schema_version=CURRENT_SCHEMA_VERSION))
    a = scan(tmp_path)[0]
    assert a.partial_tag_count is None


def test_partial_tag_count_independent_of_incomplete_state(tmp_path):
    """Both INCOMPLETE and partial-tagged at once: 2 files in dir, 1 tagged,
    MB says 5 expected. Both indicators populated independently.
    """
    album_dir = _make_album_dir(tmp_path, "Artist", "Both", n_tracks=2)
    from mutagen.mp4 import MP4

    files = sorted(album_dir.glob("*.m4a"))
    # Tag only the first file
    audio = MP4(files[0])
    audio[ATOM_MB_ALBUM_ID] = [b"rel-aaa"]
    audio.save()
    sc.write(
        album_dir,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            mb_release_id="rel-aaa",
            tagged_at=datetime.now(UTC),
            track_count_expected=5,
        ),
    )
    a = scan(tmp_path)[0]
    assert a.state == AlbumState.INCOMPLETE
    assert a.partial_tag_count == (1, 2)


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
    audio[ATOM_ALBUM] = ["Tag Album Title"]
    audio[ATOM_ARTIST] = ["Tag Artist"]
    audio.save()

    a = scan(tmp_path)[0]
    assert a.title == "Tag Album Title"
    assert a.artist == "Tag Artist"


def test_scan_falls_back_to_dir_name_when_no_album_tag(tmp_path):
    _make_album_dir(tmp_path, "Artist", "FallbackName")
    # Default sine.m4a has no ©alb tag
    a = scan(tmp_path)[0]
    assert a.title == "FallbackName"


def test_scan_skips_album_with_invalid_sidecar(tmp_path, caplog):
    """A malformed sidecar should be logged and skipped, not crash the scan."""
    good_dir = _make_album_dir(tmp_path, "Good", "Album")
    bad_dir = _make_album_dir(tmp_path, "Bad", "Album")
    sc.sidecar_path(bad_dir).write_text('{"schema_version": 99}', encoding="utf-8")
    albums = scan(tmp_path)
    paths = {a.path for a in albums}
    assert good_dir in paths
    assert bad_dir not in paths


# ---------- per-album mtime cache (opt-in) ----------


def _tag_read_spy(monkeypatch):
    """Count per-track tag reads (formats.read_scan_fields) — the expensive work
    the re-scan cache exists to skip."""
    from harmonist import formats

    reads: list[Path] = []
    real = formats.read_scan_fields

    def spy(f):
        reads.append(f)
        return real(f)

    monkeypatch.setattr("harmonist.formats.read_scan_fields", spy)
    return reads


def test_scan_without_cache_reads_tags_every_time(tmp_path, monkeypatch):
    from harmonist import scanner

    _make_album_dir(tmp_path, "Artist", "Album", n_tracks=2)
    reads = _tag_read_spy(monkeypatch)
    scanner.scan(tmp_path)
    scanner.scan(tmp_path)
    assert len(reads) == 4  # no cache → both tracks re-read on both scans


def test_scan_cache_reuses_unchanged_album(tmp_path, monkeypatch):
    from harmonist import scanner

    _make_album_dir(tmp_path, "Artist", "Album", n_tracks=2)
    reads = _tag_read_spy(monkeypatch)
    cache: scanner.AlbumCache = {}
    first = scanner.scan(tmp_path, album_cache=cache)
    second = scanner.scan(tmp_path, album_cache=cache)
    assert len(first) == len(second) == 1
    assert len(reads) == 2  # full-signature hit → second scan reads no tags


def test_scan_cache_rereads_tags_on_file_change(tmp_path, monkeypatch):
    from harmonist import scanner

    d = _make_album_dir(tmp_path, "Artist", "Album", n_tracks=1)
    reads = _tag_read_spy(monkeypatch)
    cache: scanner.AlbumCache = {}
    scanner.scan(tmp_path, album_cache=cache)
    # Bump the track's mtime to a distinct value (simulates a Picard re-tag).
    track = d / "01 Track 1.m4a"
    future = time.time() + 10
    os.utime(track, (future, future))
    scanner.scan(tmp_path, album_cache=cache)
    assert len(reads) == 2  # audio signature changed → tags re-read


def test_scan_cache_skips_tag_reads_on_sidecar_write(tmp_path, monkeypatch):
    """The cache split: a sidecar-only change reuses the cached tag fields (no
    mutagen re-read) yet STILL re-derives the Album from the new sidecar."""
    from harmonist import scanner

    d = _make_album_dir(tmp_path, "Artist", "Album", n_tracks=1)
    reads = _tag_read_spy(monkeypatch)
    cache: scanner.AlbumCache = {}
    scanner.scan(tmp_path, album_cache=cache)
    sc.write(d, Sidecar(schema_version=CURRENT_SCHEMA_VERSION, mb_release_id="rel-x"))
    second = scanner.scan(tmp_path, album_cache=cache)
    assert len(reads) == 1  # audio unchanged → tags NOT re-read (the win)
    assert second[0].sidecar is not None
    assert second[0].sidecar.mb_release_id == "rel-x"  # new sidecar still reflected


def test_scan_cache_prunes_removed_album(tmp_path):
    from harmonist import scanner

    d = _make_album_dir(tmp_path, "Artist", "Gone", n_tracks=1)
    cache: scanner.AlbumCache = {}
    scanner.scan(tmp_path, album_cache=cache)
    assert d in cache
    shutil.rmtree(d)
    scanner.scan(tmp_path, album_cache=cache)
    assert d not in cache  # stale entry pruned
