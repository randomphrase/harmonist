"""Noticing a re-tag done outside Harmonist (#220).

Harmonist asks users to re-tag in Picard. `reconcile_album` already adopts an
external re-tag — but only when the RELEASE changes, which is the rarer case. A
re-tag that keeps the release and corrects everything else left the album
deriving COMPLETE, reconcile never looking at it, and the change passing in
silence: the real case was TISM's disc numbering going from 1/1 to 2/3, which
Harmonist noticed only as "library settled after change — requesting rescan".
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from mutagen.mp4 import MP4
from PIL import Image

from harmonist import (
    activity_store,
    artwork,
    artwork_store,
    cover_art,
    derived,
    reconcile,
    tag_history,
    tagger,
)
from harmonist import sidecar as sidecar_mod
from harmonist.models import Sidecar
from harmonist.scanner import scan
from harmonist.web.reconcile_runner import reconcile_pending_orphans
from harmonist.web.scan_runner import ScanRunner
from test.test_tagger import _detail, _release_2_tracks

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SINE_M4A = FIXTURES_DIR / "sine.m4a"
MBID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def audit_db(tmp_path):
    from harmonist import activity, activity_store

    activity_store.init(tmp_path / "audit.db")
    activity.clear()
    yield


def _album(root: Path, *, tagged_at: datetime, files_at: datetime | None = None) -> Path:
    d = root / "Artist" / "Album"
    d.mkdir(parents=True)
    for i in (1, 2):
        f = d / f"{i:02d} Track {i}.m4a"
        shutil.copy(SINE_M4A, f)
        a = MP4(f)
        a["\xa9alb"] = ["Album"]
        a["\xa9ART"] = ["Artist"]
        a["trkn"] = [(i, 2)]
        a["----:com.apple.iTunes:MusicBrainz Album Id"] = [MBID.encode()]
        a.save()
    sidecar_mod.write(d, Sidecar(mb_release_id=MBID, tagged_at=tagged_at))
    if files_at is not None:
        _touch(d, files_at)
    return d


def _touch(album_dir: Path, when: datetime) -> None:
    ts = when.timestamp()
    for f in album_dir.glob("*.m4a"):
        os.utime(f, (ts, ts))


def _run(tmp_path, **kw):
    return reconcile_pending_orphans(tmp_path, fetch_urls=lambda m: [], rate_limit_seconds=0, **kw)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_files_newer_than_the_tagging_look_externally_retagged(tmp_path):
    old = datetime.now(UTC) - timedelta(days=30)
    _album(tmp_path, tagged_at=old, files_at=datetime.now(UTC))

    assert reconcile.looks_externally_retagged(scan(tmp_path)[0])


def test_files_older_than_the_tagging_do_not(tmp_path):
    """The ordinary case: Harmonist writes the files, then the sidecar, so its
    own tagging always leaves the sidecar a shade newer."""
    now = datetime.now(UTC)
    _album(tmp_path, tagged_at=now, files_at=now - timedelta(hours=1))

    assert not reconcile.looks_externally_retagged(scan(tmp_path)[0])


def test_a_hair_of_clock_skew_does_not_count(tmp_path):
    """A filesystem with coarse timestamps, or a slow network mount, can invert
    the write order by a fraction. `RETAG_MARGIN` absorbs that."""
    now = datetime.now(UTC)
    _album(tmp_path, tagged_at=now, files_at=now + timedelta(seconds=1))

    assert not reconcile.looks_externally_retagged(scan(tmp_path)[0])


def test_an_album_that_was_never_tagged_is_not_a_candidate(tmp_path):
    """No `tagged_at` means nothing to be newer than."""
    d = tmp_path / "Artist" / "Album"
    d.mkdir(parents=True)
    shutil.copy(SINE_M4A, d / "01 Track.m4a")
    sidecar_mod.write(d, Sidecar(mb_release_id=MBID))

    assert not reconcile.looks_externally_retagged(scan(tmp_path)[0])


# ---------------------------------------------------------------------------
# Adoption
# ---------------------------------------------------------------------------


def test_the_pass_reports_it_once_and_adopts_the_change(tmp_path, audit_db):
    from harmonist import activity_store

    written = datetime.now(UTC).replace(microsecond=0)
    d = _album(tmp_path, tagged_at=written - timedelta(days=30), files_at=written)

    stats = _run(tmp_path)

    assert stats["adopted_external"] == 1
    assert sidecar_mod.read(d).tagged_at == written, "the sidecar follows the files"
    messages = [e.message for e in activity_store.recent(50)]
    assert any("re-tagged outside Harmonist" in m for m in messages)


def test_a_second_pass_says_nothing(tmp_path, audit_db):
    """Self-clearing: once `tagged_at` describes the tags on disk, the album no
    longer looks externally re-tagged. Reconcile runs on startup and after every
    sync, so a notice that repeated would be a notice nobody reads."""
    written = datetime.now(UTC).replace(microsecond=0)
    _album(tmp_path, tagged_at=written - timedelta(days=30), files_at=written)

    first = _run(tmp_path)
    second = _run(tmp_path)

    assert (first["adopted_external"], second["adopted_external"]) == (1, 0)


def test_it_keeps_everything_except_the_timestamp(tmp_path, audit_db):
    """A re-tag changed the tags, not the album's identity, its store link, or
    any decision recorded about it."""
    written = datetime.now(UTC).replace(microsecond=0)
    d = _album(tmp_path, tagged_at=written - timedelta(days=30), files_at=written)
    sc = sidecar_mod.read(d)
    assert sc is not None
    sc.store_url = "https://x.bandcamp.com/album/y"
    sc.tracks_unavailable = True
    sidecar_mod.write(d, sc)
    _touch(d, written)

    _run(tmp_path)

    after = sidecar_mod.read(d)
    assert after.mb_release_id == MBID
    assert after.store_url == "https://x.bandcamp.com/album/y"
    assert after.tracks_unavailable is True


def test_a_forgotten_album_is_left_alone(tmp_path, audit_db):
    written = datetime.now(UTC).replace(microsecond=0)
    d = _album(tmp_path, tagged_at=written - timedelta(days=30), files_at=written)

    assert _run(tmp_path, exempt_paths={d})["adopted_external"] == 0


def test_the_notice_names_the_album(tmp_path, audit_db):
    from harmonist import activity_store

    written = datetime.now(UTC).replace(microsecond=0)
    _album(tmp_path, tagged_at=written - timedelta(days=30), files_at=written)

    _run(tmp_path)

    labels = [e.album_label for e in activity_store.recent(50) if e.album_label]
    assert "Artist — Album" in labels


# ---------------------------------------------------------------------------
# Harmonist's own writes (#979)
# ---------------------------------------------------------------------------
#
# Writing artwork, and undoing a tagging or an artwork change, write the files
# without moving `tagged_at`, so by mtime alone each looks like a Picard re-tag.
# These go through the real writers: the check was first proved with an audit
# line written by hand, which agreed with its own list of names and nothing
# else — and the artwork action, which arrived later with lines of its own, was
# reported as a re-tag outside Harmonist after every write.


@pytest.fixture
def stores(tmp_path, audit_db):
    artwork_store.configure(tmp_path / "kept")
    derived.reset()
    yield
    derived.reset()


def _front() -> cover_art.Front:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), "red").save(buf, "JPEG")
    return cover_art.Front(data=buf.getvalue(), mime="image/jpeg")


def _tagged_long_ago(root: Path) -> Path:
    """Tagged a month ago, files untouched since: nothing to notice yet."""
    now = datetime.now(UTC)
    return _album(root, tagged_at=now - timedelta(days=30), files_at=now - timedelta(days=31))


def _apply_artwork(album_dir: Path) -> None:
    files = sorted(album_dir.glob("*.m4a"))
    archive = _front()
    plan = tagger.decide_artwork(
        album_dir,
        files,
        None,
        archive=archive,
        choices={artwork.COVER: artwork.Choice.ARCHIVE},
    )
    outcome = tagger.apply_artwork(album_dir, plan, files=files, cover_path=None, archive=archive)
    assert outcome.written and not (outcome.stale or outcome.unkept or outcome.failed)


def _forget_history(root: Path) -> None:
    """A fresh store, so that only what comes next can account for the files."""
    activity_store.init(root / "later.db")


def _artwork_applied(root: Path, album_with_tracks: Callable[..., Path]) -> None:
    _apply_artwork(_tagged_long_ago(root))


def _artwork_undone(root: Path, album_with_tracks: Callable[..., Path]) -> None:
    d = _tagged_long_ago(root)
    _apply_artwork(d)
    plan = tag_history.artwork_revert_plan(_detail())
    _forget_history(root)
    assert tagger.restore_artwork(d, plan).removed


def _tags_undone(root: Path, album_with_tracks: Callable[..., Path]) -> None:
    d = album_with_tracks(2)
    release = _release_2_tracks()
    tagger.tag_album(d, release)
    count = len(_detail())
    release["title"] = "Revised album"
    tagger.tag_album(d, release)
    plan = tag_history.revert_plan(_detail()[count:])
    now = datetime.now(UTC)
    sidecar_mod.write(d, Sidecar(mb_release_id=release["id"], tagged_at=now - timedelta(days=30)))
    _touch(d, now - timedelta(days=31))
    _forget_history(root)
    assert tagger.revert_tags(d, plan).files == 2


@pytest.mark.parametrize(
    "write",
    [_artwork_applied, _artwork_undone, _tags_undone],
    ids=["artwork", "artwork-undo", "tag-undo"],
)
def test_harmonists_own_write_is_not_reported_as_external(
    tmp_path, stores, album_with_tracks, write
):
    """Reporting the user's own artwork change, or their own Undo, back to them
    as somebody else's re-tag is the confident lie this codebase spends its
    time not telling. Twice, because the album stays a candidate: nothing
    moves `tagged_at` for a write that is not a tagging."""
    write(tmp_path, album_with_tracks)
    assert reconcile.looks_externally_retagged(scan(tmp_path)[0]), "files newer than tagging"

    passes = [_run(tmp_path)["adopted_external"] for _ in range(2)]

    assert passes == [0, 0]
    messages = [e.message for e in activity_store.recent(50)]
    assert not any("re-tagged outside Harmonist" in m for m in messages)


def test_a_retag_after_harmonists_own_write_is_still_noticed(tmp_path, stores):
    """Harmonist's write accounts for the files only as they were when it was
    made. Silencing the album for any write since `tagged_at` would let one
    artwork update hide every later Picard re-tag for good, since nothing moves
    `tagged_at` again until something adopts one."""
    d = _tagged_long_ago(tmp_path)
    _apply_artwork(d)
    _touch(d, datetime.now(UTC) + timedelta(minutes=1))

    assert _run(tmp_path)["adopted_external"] == 1


# ---------------------------------------------------------------------------
# Through the background scan (#230)
# ---------------------------------------------------------------------------
#
# Everything above scans via `scanner.scan()`. Production does not: it reads the
# snapshot the background `ScanRunner` builds, which inlined a stale copy of
# `resolve_dir` and never stamped `files_written_at` — so detection was dead in
# the field while this file stayed green. These go through the runner.


async def _scan_via_runner(runner: ScanRunner) -> None:
    runner.attach_loop()
    for _ in range(300):
        if runner.has_completed():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("background scan never completed")


def test_the_background_scan_notices_it_too(tmp_path):
    """The snapshot reconcile actually reads is the background runner's, so a
    re-tag has to be visible in THAT album, not just in a synchronous scan."""
    _album(tmp_path, tagged_at=datetime.now(UTC) - timedelta(days=30), files_at=datetime.now(UTC))
    runner = ScanRunner(tmp_path)

    asyncio.run(_scan_via_runner(runner))

    assert reconcile.looks_externally_retagged(runner.albums()[0])


def test_the_background_scans_cache_does_not_poison_the_sync_path(tmp_path):
    """Both paths share one mtime cache, so an album built without its timestamp
    doesn't just break the background scan — `scan_now()` hands the very same
    object back on a signature hit, and inherits the blindness."""
    _album(tmp_path, tagged_at=datetime.now(UTC) - timedelta(days=30), files_at=datetime.now(UTC))
    runner = ScanRunner(tmp_path)

    asyncio.run(_scan_via_runner(runner))  # populates the shared cache
    albums = runner.scan_now()  # cache hit — returns what the scan built

    assert reconcile.looks_externally_retagged(albums[0])
