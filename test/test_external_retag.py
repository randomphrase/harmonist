"""Noticing a re-tag done outside Harmonist (#220).

Harmonist asks users to re-tag in Picard. `reconcile_album` already adopts an
external re-tag — but only when the RELEASE changes, which is the rarer case. A
re-tag that keeps the release and corrects everything else left the album
deriving COMPLETE, reconcile never looking at it, and the change passing in
silence: the real case was TISM's disc numbering going from 1/1 to 2/3, which
Harmonist noticed only as "library settled after change — requesting rescan".
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from mutagen.mp4 import MP4

from harmonist import reconcile
from harmonist import sidecar as sidecar_mod
from harmonist.models import Sidecar
from harmonist.scanner import scan
from harmonist.web.reconcile_runner import reconcile_pending_orphans

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


def test_harmonists_own_write_is_not_reported_as_external(tmp_path, audit_db):
    """`revert_tags` and `restore_artwork` write files without moving
    `tagged_at`, so by mtime alone an undo is indistinguishable from a Picard
    re-tag. Reporting the user's own undo back to them as somebody else's change
    is the confident lie this codebase spends its time not telling."""
    from harmonist import audit

    written = datetime.now(UTC).replace(microsecond=0)
    d = _album(tmp_path, tagged_at=written - timedelta(days=30), files_at=written)
    # As `revert_tags` would have left it: an audited write, no tagged_at move.
    audit.record("tag.revert", album_id=MBID, album=d, files=2, fields=1, stale=0)

    stats = _run(tmp_path)

    assert stats["adopted_external"] == 0
    assert sidecar_mod.read(d).tagged_at != written, "left alone"


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
