"""Backfilling `track_count_expected` on adopted albums (#187).

Onboarding derives a sidecar from the file tags and deliberately leaves the
expected track count unset — filling it in there costs one rate-limited MB call
per album. Nothing filled it in later, so INCOMPLETE could never be derived for
an adopted library and the Library's Incomplete filter reported zero for a shelf
full of half-albums.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from mutagen.mp4 import MP4

from harmonist import reconcile
from harmonist import sidecar as sidecar_mod
from harmonist.models import AlbumState, Sidecar
from harmonist.scanner import scan
from harmonist.web.reconcile_runner import reconcile_pending_orphans

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SINE_M4A = FIXTURES_DIR / "sine.m4a"

MBID = "3da95637-a647-42d6-a1e7-81a20c9dbfd4"


@pytest.fixture
def audit_db(tmp_path):
    """The reconcile pass opens action scopes and records activity, so the
    store has to exist for it. Per-test file, so runs can't see each other."""
    from harmonist import activity, activity_store

    activity_store.init(tmp_path / "audit.db")
    activity.clear()
    yield


def _adopted_album(root: Path, name: str = "Album", *, tracks: int = 2, mbid: str = MBID) -> Path:
    """An album as ONBOARDING leaves it: tagged files, a sidecar naming the
    release, and no `track_count_expected` — exactly `reconcile_album`'s output.
    """
    d = root / "Artist" / name
    d.mkdir(parents=True)
    for i in range(1, tracks + 1):
        f = d / f"{i:02d} Track {i}.m4a"
        shutil.copy(SINE_M4A, f)
        audio = MP4(f)
        audio["\xa9alb"] = [name]
        audio["\xa9ART"] = ["Artist"]
        audio["----:com.apple.iTunes:MusicBrainz Album Id"] = [mbid.encode()]
        audio.save()
    sidecar_mod.write(
        d,
        Sidecar(
            mb_release_id=mbid,
            added_at=datetime(2026, 1, 1, tzinfo=UTC),
            tagged_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    )
    return d


def _counter(count: int):
    """A stub lookup that records how many times it was asked."""
    calls: list[str] = []

    def fetch(mbid: str) -> int:
        calls.append(mbid)
        return count

    fetch.calls = calls
    return fetch


# ---------------------------------------------------------------------------
# The gap itself
# ---------------------------------------------------------------------------


def test_an_adopted_album_starts_with_no_expected_track_count(tmp_path):
    """The defect, stated as a test: half an album reads as COMPLETE."""
    _adopted_album(tmp_path, tracks=2)

    album = scan(tmp_path)[0]

    assert album.sidecar.track_count_expected is None
    assert album.state == AlbumState.COMPLETE, "nothing to compare 2 files against"
    assert reconcile.needs_track_count(album)


def test_a_tagged_album_that_already_has_a_count_is_not_a_candidate(tmp_path):
    d = _adopted_album(tmp_path)
    sc = sidecar_mod.read(d)
    sidecar_mod.write(d, Sidecar(**{**sc.__dict__, "track_count_expected": 2}))

    assert not reconcile.needs_track_count(scan(tmp_path)[0])


def test_an_untagged_album_is_not_a_candidate(tmp_path):
    """No release to ask MusicBrainz about."""
    d = tmp_path / "Artist" / "Album"
    d.mkdir(parents=True)
    shutil.copy(SINE_M4A, d / "01 Track.m4a")

    assert not reconcile.needs_track_count(scan(tmp_path)[0])


# ---------------------------------------------------------------------------
# The backfill
# ---------------------------------------------------------------------------


def test_backfill_records_the_releases_track_count(tmp_path):
    d = _adopted_album(tmp_path, tracks=2)

    assert reconcile.backfill_track_count(d, fetch_count=lambda m: 21) == 21
    assert sidecar_mod.read(d).track_count_expected == 21


def test_a_backfilled_album_derives_incomplete(tmp_path):
    """The point of the whole exercise: the Library can finally see it."""
    d = _adopted_album(tmp_path, tracks=11)
    reconcile.backfill_track_count(d, fetch_count=lambda m: 21)

    album = scan(tmp_path)[0]

    assert album.state == AlbumState.INCOMPLETE
    assert (album.track_count, album.sidecar.track_count_expected) == (11, 21)


def test_a_genuinely_complete_album_stays_complete(tmp_path):
    d = _adopted_album(tmp_path, tracks=2)
    reconcile.backfill_track_count(d, fetch_count=lambda m: 2)

    assert scan(tmp_path)[0].state == AlbumState.COMPLETE


def test_backfill_keeps_the_rest_of_the_sidecar(tmp_path):
    """It records one number; it must not quietly rewrite an album's identity,
    store link or history."""
    d = _adopted_album(tmp_path)
    before = sidecar_mod.read(d)

    reconcile.backfill_track_count(d, fetch_count=lambda m: 9)

    after = sidecar_mod.read(d)
    assert after.track_count_expected == 9
    for field in ("mb_release_id", "store_url", "added_at", "tagged_at", "bandcamp"):
        assert getattr(after, field) == getattr(before, field), field


def test_backfill_is_a_no_op_second_time(tmp_path):
    d = _adopted_album(tmp_path)
    fetch = _counter(21)

    first = reconcile.backfill_track_count(d, fetch_count=fetch)
    second = reconcile.backfill_track_count(d, fetch_count=fetch)

    assert (first, second) == (21, None)
    assert len(fetch.calls) == 1, "the second pass must not re-ask MusicBrainz"


def test_backfill_re_reads_the_sidecar_rather_than_trusting_a_stale_snapshot(tmp_path):
    """A scan is minutes old on a large library. If a tag landed in between,
    this write must not clobber the newer sidecar."""
    d = _adopted_album(tmp_path)
    sidecar_mod.write(d, Sidecar(mb_release_id="other-mbid", track_count_expected=5))

    assert reconcile.backfill_track_count(d, fetch_count=lambda m: 21) is None
    assert sidecar_mod.read(d).track_count_expected == 5


def test_a_failed_lookup_writes_nothing(tmp_path):
    """A failed lookup must not be recorded as a successful one — a wrong count
    mislabels the album permanently."""
    from harmonist.mb_lookup import MBError

    d = _adopted_album(tmp_path)

    def boom(mbid: str) -> int:
        raise MBError("MB is down")

    with pytest.raises(MBError):
        reconcile.backfill_track_count(d, fetch_count=boom)
    assert sidecar_mod.read(d).track_count_expected is None


# ---------------------------------------------------------------------------
# Through the reconcile pass
# ---------------------------------------------------------------------------


def _run(tmp_path, fetch_count, **kw):
    return reconcile_pending_orphans(
        tmp_path,
        fetch_urls=lambda m: [],
        fetch_track_count=fetch_count,
        rate_limit_seconds=0,
        **kw,
    )


def test_the_reconcile_pass_backfills_every_adopted_album(tmp_path, audit_db):
    _adopted_album(tmp_path, "One", tracks=11)
    _adopted_album(tmp_path, "Two", tracks=2)

    stats = _run(tmp_path, lambda m: 21)

    assert stats["total"] == 0, "no orphans — this runs on the early-return path"
    assert stats["counted"] == 2
    assert stats["newly_incomplete"] == 2
    assert {a.state for a in scan(tmp_path)} == {AlbumState.INCOMPLETE}


def test_the_pass_counts_only_the_albums_that_are_short(tmp_path, audit_db):
    _adopted_album(tmp_path, "Short", tracks=1)
    _adopted_album(tmp_path, "Whole", tracks=3)

    stats = _run(tmp_path, lambda m: 3)

    assert (stats["counted"], stats["newly_incomplete"]) == (2, 1)


def test_a_second_pass_asks_musicbrainz_nothing(tmp_path, audit_db):
    """Bounded by the albums lacking the field, and each success removes itself
    from the candidate set — so this is a long first pass and nothing after."""
    _adopted_album(tmp_path, tracks=2)
    fetch = _counter(21)

    _run(tmp_path, fetch)
    second = _run(tmp_path, fetch)

    assert len(fetch.calls) == 1
    assert second["counted"] == 0


def test_omitting_the_lookup_skips_the_backfill_entirely(tmp_path, audit_db):
    """`None` means skip, never "fall back to the real MusicBrainz" — the trap
    that had the test suite quietly making live requests."""
    d = _adopted_album(tmp_path)

    stats = reconcile_pending_orphans(tmp_path, fetch_urls=lambda m: [], rate_limit_seconds=0)

    assert stats["counted"] == 0
    assert sidecar_mod.read(d).track_count_expected is None


def test_a_failed_lookup_leaves_the_album_for_the_next_pass(tmp_path, audit_db):
    d = _adopted_album(tmp_path)
    attempts = []

    def flaky(mbid: str) -> int:
        attempts.append(mbid)
        if len(attempts) == 1:
            raise RuntimeError("network")
        return 21

    assert _run(tmp_path, flaky)["counted"] == 0
    assert sidecar_mod.read(d).track_count_expected is None

    assert _run(tmp_path, flaky)["counted"] == 1
    assert sidecar_mod.read(d).track_count_expected == 21


def test_a_forgotten_album_is_not_backfilled(tmp_path, audit_db):
    d = _adopted_album(tmp_path)

    stats = _run(tmp_path, lambda m: 21, exempt_paths={d})

    assert stats["counted"] == 0
    assert sidecar_mod.read(d).track_count_expected is None


def test_the_pass_announces_itself_and_names_the_incomplete_albums(tmp_path, audit_db):
    from harmonist import activity_store

    _adopted_album(tmp_path, "Short", tracks=11)

    _run(tmp_path, lambda m: 21)

    messages = [e.message for e in activity_store.recent(50)]
    assert any("expected track counts — 1 album(s)" in m for m in messages)
    assert any("Missing 10 of 21 tracks" in m for m in messages)
    assert any("1 turned out to be incomplete" in m for m in messages)


def test_the_count_is_audited_on_the_albums_own_history(tmp_path, audit_db):
    """The sidecar write audits itself — `track_count_expected` is load-bearing,
    so a change to it reclassifies the album and has to be reconstructable."""
    from harmonist import activity_store

    _adopted_album(tmp_path, tracks=11)

    _run(tmp_path, lambda m: 21)

    messages = [e.message for e in activity_store.recent(50)]
    assert any("sidecar.update" in m and "track_count_expected=None->21" in m for m in messages)
