"""Tests for the sidecar-driven scanner."""

from __future__ import annotations

import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

from harmonist import formats, tagger
from harmonist import sidecar as sc
from harmonist.models import (
    AlbumState,
    BandcampInfo,
    MatchCandidate,
    Sidecar,
)
from harmonist.scanner import scan
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
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=None),
            mb_release_id="rel-aaa",
            tagged_at=datetime.now(UTC),
        ),
    )
    a = scan(tmp_path)[0]
    assert a.state == AlbumState.NEEDS_SYNC


def test_scan_purchase_unavailable_is_complete_not_needs_sync(tmp_path):
    """purchase_unavailable (a withdrawn/ripped/elsewhere album the user accepted) is
    terminal COMPLETE, not NEEDS_SYNC — so a full sync never re-surrenders it."""
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
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=None),
            mb_release_id="rel-aaa",
            tagged_at=datetime.now(UTC),
            purchase_unavailable=True,
        ),
    )
    a = scan(tmp_path)[0]
    assert a.state == AlbumState.COMPLETE


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
            mb_release_id="rel-aaa",
            tagged_at=datetime.now(UTC),
        ),
    )
    a = scan(tmp_path)[0]
    assert a.state == AlbumState.COMPLETE


def _tag_files(album_dir, mbid: str = "rel-aaa", **totals) -> None:
    """Stamp the MB Album Id, and optionally the trkn/disk totals, on every file."""
    from mutagen.mp4 import MP4

    from test.helpers import write_track_totals

    for f in sorted(album_dir.glob("*.m4a")):
        audio = MP4(f)
        audio[ATOM_MB_ALBUM_ID] = [mbid.encode()]
        audio.save()
    if totals:
        write_track_totals(album_dir, **totals)


def _tagged_sidecar(album_dir, mbid: str = "rel-aaa") -> None:
    sc.write(album_dir, Sidecar(mb_release_id=mbid, tagged_at=datetime.now(UTC)))


def test_scan_complete_when_the_files_say_every_track_is_here(tmp_path):
    """The files' own `trkn` total == file count → COMPLETE (#195)."""
    album_dir = _make_album_dir(tmp_path, "Artist", "Album", n_tracks=2)
    _tag_files(album_dir, track_total=2)
    _tagged_sidecar(album_dir)

    assert scan(tmp_path)[0].state == AlbumState.COMPLETE


def test_scan_incomplete_when_the_files_say_tracks_are_missing(tmp_path):
    """`trkn` says 5 tracks; 2 are on disk → INCOMPLETE, with no MB lookup."""
    album_dir = _make_album_dir(tmp_path, "Artist", "Album", n_tracks=2)
    _tag_files(album_dir, track_total=5)
    _tagged_sidecar(album_dir)

    album = scan(tmp_path)[0]
    assert album.state == AlbumState.INCOMPLETE
    assert album.expected_track_count == 5


def test_expected_count_sums_the_discs_of_a_multi_disc_release(tmp_path):
    """A 2-disc release of 11 + 10 is 21 tracks, and every one of them is
    stated by the files themselves — the Vapourized case from #16."""
    album_dir = _make_album_dir(tmp_path, "Artist", "Album", n_tracks=0)
    for sub, n, disc, total in (("CD1", 3, 1, 3), ("CD2", 2, 2, 2)):
        (album_dir / sub).mkdir()
        for i in range(1, n + 1):
            shutil.copy(SINE_M4A, album_dir / sub / f"{i:02d} Track.m4a")
        _tag_files(album_dir / sub, track_total=total, disc_num=disc, disc_total=2)
        _tagged_sidecar(album_dir / sub)

    albums = scan(tmp_path)

    assert len(albums) == 1, "identity grouping folds the two discs into one album"
    assert albums[0].expected_track_count == 5
    assert albums[0].state == AlbumState.COMPLETE


def test_a_release_missing_a_whole_disc_is_incomplete_with_no_known_total(tmp_path):
    """`disk` says 2 discs and only disc 1 has files. Certainly incomplete —
    but nothing on disk records how long disc 2 was, so there is no total to
    show. `None` here must NOT read as "no information, assume complete"."""
    album_dir = _make_album_dir(tmp_path, "Artist", "Album", n_tracks=2)
    _tag_files(album_dir, track_total=2, disc_num=1, disc_total=2)
    _tagged_sidecar(album_dir)

    album = scan(tmp_path)[0]
    assert album.state == AlbumState.INCOMPLETE
    assert album.expected_track_count is None


def test_scan_complete_when_the_files_carry_no_totals(tmp_path):
    """Files tagged by something that wrote no track totals: unknown, and
    unknown must not be reported as missing tracks."""
    album_dir = _make_album_dir(tmp_path, "Artist", "Album", n_tracks=2)
    _tag_files(album_dir)  # MBID only, no trkn/disk
    _tagged_sidecar(album_dir)

    album = scan(tmp_path)[0]
    assert album.state == AlbumState.COMPLETE
    assert album.expected_track_count is None


def test_scan_incomplete_promotes_to_complete_on_file_addition(tmp_path):
    """Drop the missing track in and the next scan promotes it. Pure
    derivation — no sidecar mutation, and now no lookup either."""
    album_dir = _make_album_dir(tmp_path, "Artist", "Album", n_tracks=2)
    _tag_files(album_dir, track_total=3)
    _tagged_sidecar(album_dir)
    assert scan(tmp_path)[0].state == AlbumState.INCOMPLETE

    third = album_dir / "03 Track 3.m4a"
    shutil.copy(SINE_M4A, third)
    _tag_files(album_dir, track_total=3)

    assert scan(tmp_path)[0].state == AlbumState.COMPLETE


def test_partial_tag_count_independent_of_incomplete_state(tmp_path):
    """Both INCOMPLETE and partial-tagged at once: 2 files in dir, 1 tagged,
    MB says 5 expected. Both indicators populated independently.
    """
    from mutagen.mp4 import MP4

    from test.helpers import write_track_totals

    album_dir = _make_album_dir(tmp_path, "Artist", "Both", n_tracks=2)
    # Both files carry the release's totals — that is an album-level fact the
    # tagging writes everywhere — but only the first carries the MB Album Id.
    write_track_totals(album_dir, track_total=5)
    audio = MP4(min(album_dir.glob("*.m4a")))
    audio[ATOM_MB_ALBUM_ID] = [b"rel-aaa"]
    audio.save()
    _tagged_sidecar(album_dir)

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
    sc.write(d, Sidecar(mb_release_id="rel-x"))
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


# ---------- an unreadable file is not an untagged one (#112) ----------


def _make_unreadable(path: Path) -> None:
    """Corrupt a file so mutagen can't open it — the observable shape of a
    permission error, a truncation, or a disk starting to fail."""
    path.write_bytes(b"not audio at all")


def test_a_file_that_cannot_be_read_is_flagged_rather_than_read_as_untagged(tmp_path):
    """The root of #112: an unopenable file returned all-None fields, which is
    byte-identical to a readable file carrying no tags."""
    d = _make_album_dir(tmp_path, "Artist", "Corrupt", n_tracks=1)
    f = min(d.glob("*.m4a"))
    _make_unreadable(f)

    fields = formats.read_scan_fields(f)
    assert fields.unreadable is True
    assert fields.album_id is None  # …and it still looks empty, which is the trap


def test_a_readable_untagged_file_is_not_flagged_unreadable(tmp_path):
    """The control. Without it the flag could simply be always-on."""
    d = _make_album_dir(tmp_path, "Artist", "Untagged", n_tracks=1)
    fields = formats.read_scan_fields(min(d.glob("*.m4a")))
    assert fields.unreadable is False
    assert fields.album_id is None


def _tag_with_mbid(album_dir: Path, mbid: str) -> None:
    from mutagen.mp4 import MP4

    for f in sorted(album_dir.glob("*.m4a")):
        audio = MP4(f)
        audio[ATOM_MB_ALBUM_ID] = [mbid.encode()]
        audio.save()


def test_an_unreadable_track_makes_the_album_incomplete(tmp_path, caplog):
    """A track Harmonist can't open is, for every purpose the user cares about,
    a track they don't have — so it lands in the same state as an absent one.

    It must NOT read as TAGGING (the original bug), which invites a re-tag —
    a write to the drive that just failed a read."""
    d = _make_album_dir(tmp_path, "Artist", "Failing", n_tracks=3)
    _tag_with_mbid(d, "rel-failing")
    sc.write(d, Sidecar(mb_release_id="rel-failing", tagged_at=datetime.now(UTC)))
    assert scan(tmp_path)[0].state == AlbumState.COMPLETE  # before the disk trouble

    _make_unreadable(min(d.glob("*.m4a")))

    with caplog.at_level("WARNING"):
        album = scan(tmp_path)[0]

    assert album.state == AlbumState.INCOMPLETE
    assert any("could not be read" in r.message for r in caplog.records)


def test_one_corrupt_track_is_not_hidden_by_the_others_being_fine(tmp_path):
    """The ordering that matters: the check runs BEFORE the tagged/untagged
    branch. Two good files still carry the MBID, so a later check would call the
    album COMPLETE and the corruption would never be shown."""
    d = _make_album_dir(tmp_path, "Artist", "OneBad", n_tracks=3)
    _tag_with_mbid(d, "rel-onebad")
    sc.write(d, Sidecar(mb_release_id="rel-onebad", tagged_at=datetime.now(UTC)))

    _make_unreadable(min(d.glob("*.m4a")))

    assert scan(tmp_path)[0].state == AlbumState.INCOMPLETE


def test_a_genuinely_untagged_album_still_reads_as_tagging(tmp_path):
    """Control for the fix: unreadable files must stop the downgrade, but a
    readable album that really isn't tagged yet still has to reach TAGGING, or
    the fix would strand albums mid-pipeline."""
    d = _make_album_dir(tmp_path, "Artist", "NotYet", n_tracks=2)
    sc.write(d, Sidecar(mb_release_id="rel-notyet", tagged_at=datetime.now(UTC)))
    assert scan(tmp_path)[0].state == AlbumState.TAGGING


def test_expected_count_ignores_a_disc_whose_files_disagree(tmp_path):
    """Files mid-retag disagree on the total. Averaging or picking a winner
    would invent a number; "don't know" is the honest answer, and an album is
    not accused of missing tracks on the strength of it."""
    from mutagen.mp4 import MP4

    album_dir = _make_album_dir(tmp_path, "Artist", "Album", n_tracks=2)
    _tag_files(album_dir)
    for f, total in zip(sorted(album_dir.glob("*.m4a")), (5, 9), strict=True):
        audio = MP4(f)
        audio["trkn"] = [(1, total)]
        audio.save()
    _tagged_sidecar(album_dir)

    album = scan(tmp_path)[0]
    assert album.expected_track_count is None
    assert album.state == AlbumState.COMPLETE


def test_expected_count_survives_more_files_than_the_release_lists(tmp_path):
    """A bonus track the release doesn't have must not read as INCOMPLETE —
    `complete` is "every expected track is here", not "the counts are equal"."""
    album_dir = _make_album_dir(tmp_path, "Artist", "Album", n_tracks=3)
    _tag_files(album_dir, track_total=2)
    _tagged_sidecar(album_dir)

    assert scan(tmp_path)[0].state == AlbumState.COMPLETE


def test_an_unreadable_file_does_not_vote_on_the_expected_count(tmp_path):
    """`unreadable` files already force INCOMPLETE on their own (#112); they
    must not also corrupt the total with the None every field reads as."""
    from harmonist import formats, scanner

    fields = [
        formats.ScanFields(None, None, None, None, unreadable=True),
        formats.ScanFields("A", "rel", "X", "ALAC", track_total=2, disc_num=1, disc_total=1),
        formats.ScanFields("A", "rel", "X", "ALAC", track_total=2, disc_num=1, disc_total=1),
    ]

    assert scanner.expected_tracks(fields) == scanner.ExpectedTracks(total=2, complete=True)


def test_expected_count_needs_no_musicbrainz_call(tmp_path, monkeypatch):
    """The point of #195. A scan of an adopted album derives its expected count
    with the network unavailable — where the backfill this replaced spent one
    rate-limited request per album."""
    import harmonist.mb_lookup as mb

    def boom(*a, **kw):
        raise AssertionError("the scan must not reach MusicBrainz")

    for name in ("fetch_release", "fetch_release_urls", "lookup_by_bandcamp_url"):
        monkeypatch.setattr(mb, name, boom)

    album_dir = _make_album_dir(tmp_path, "Artist", "Album", n_tracks=2)
    _tag_files(album_dir, track_total=5)
    _tagged_sidecar(album_dir)

    album = scan(tmp_path)[0]
    assert (album.state, album.expected_track_count) == (AlbumState.INCOMPLETE, 5)


def _video_disc(album_dir, disc: int, disc_total: int, n: int, track_total: int) -> None:
    """Lay down `n` Picard-tagged .m4v files for one medium of a release."""
    from mutagen.mp4 import MP4

    for i in range(1, n + 1):
        f = album_dir / f"{disc}-{i:02d} Video {i}.m4v"
        shutil.copy(SINE_M4A, f)  # an MP4 container either way
        audio = MP4(f)
        audio[ATOM_MB_ALBUM_ID] = [b"rel-aaa"]
        audio["trkn"] = [(i, track_total)]
        audio["disk"] = [(disc, disc_total)]
        audio.save()


def test_a_video_medium_on_disk_completes_the_album(tmp_path):
    """The Barking case (#193): 9 CD tracks and 9 DVD ones, every file present,
    reported as "missing 9 of 18" because the DVD half is not audio."""
    album_dir = _make_album_dir(tmp_path, "Underworld", "Barking", n_tracks=9)
    _tag_files(album_dir, track_total=9, disc_num=1, disc_total=2)
    _video_disc(album_dir, disc=2, disc_total=2, n=9, track_total=9)
    _tagged_sidecar(album_dir)

    album = scan(tmp_path)[0]
    assert album.expected_track_count == 18
    assert album.state == AlbumState.COMPLETE


def test_a_video_medium_never_ripped_stays_incomplete(tmp_path):
    """The other half of the same rule. No files of any kind for the medium, so
    the album really is short and must keep saying so."""
    album_dir = _make_album_dir(tmp_path, "Artist", "Album", n_tracks=9)
    _tag_files(album_dir, track_total=9, disc_num=1, disc_total=2)
    _tagged_sidecar(album_dir)

    assert scan(tmp_path)[0].state == AlbumState.INCOMPLETE


def test_a_partly_ripped_video_medium_is_incomplete(tmp_path):
    """7 of the DVD's 9 videos present — counted, and still short."""
    album_dir = _make_album_dir(tmp_path, "Artist", "Album", n_tracks=9)
    _tag_files(album_dir, track_total=9, disc_num=1, disc_total=2)
    _video_disc(album_dir, disc=2, disc_total=2, n=7, track_total=9)
    _tagged_sidecar(album_dir)

    album = scan(tmp_path)[0]
    assert album.expected_track_count == 18
    assert album.state == AlbumState.INCOMPLETE


def test_video_files_are_not_offered_to_anything_that_tags(tmp_path):
    """Harmonist cannot write these (#66). They must stay out of `audio_files`,
    which is what the tagger, the matcher and the cover reader consume."""
    from harmonist import album_files

    album_dir = _make_album_dir(tmp_path, "Artist", "Album", n_tracks=2)
    _video_disc(album_dir, disc=2, disc_total=2, n=2, track_total=2)

    assert all(f.suffix == ".m4a" for f in album_files.audio_files(album_dir))
    assert all(f.suffix == ".m4v" for f in album_files.video_files(album_dir))


def test_a_new_video_file_invalidates_the_scan_cache(tmp_path):
    """Videos change what the album derives, so a signature blind to them would
    serve a stale Album after a DVD rip landed."""
    album_dir = _make_album_dir(tmp_path, "Artist", "Album", n_tracks=9)
    _tag_files(album_dir, track_total=9, disc_num=1, disc_total=2)
    _tagged_sidecar(album_dir)
    cache: dict = {}
    assert scan(tmp_path, album_cache=cache)[0].state == AlbumState.INCOMPLETE

    _video_disc(album_dir, disc=2, disc_total=2, n=9, track_total=9)

    assert scan(tmp_path, album_cache=cache)[0].state == AlbumState.COMPLETE
