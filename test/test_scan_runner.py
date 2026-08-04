"""Tests for the asyncio background scan runner (web/scan_runner.py)."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from harmonist.web.scan_runner import ScanRunner

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SINE_M4A = FIXTURES_DIR / "sine.m4a"


def _album(root: Path, name: str) -> Path:
    d = root / "Artist" / name
    d.mkdir(parents=True)
    shutil.copy(SINE_M4A, d / "01 Track.m4a")
    return d


async def _wait(predicate, *, timeout_ticks: int = 300) -> None:
    for _ in range(timeout_ticks):
        if predicate():
            return
        await asyncio.sleep(0.01)


def test_scan_runner_not_engaged_before_attach(tmp_path):
    runner = ScanRunner(tmp_path)
    assert runner.is_engaged() is False
    assert runner.albums() == []
    assert runner.status()["state"] == "idle"
    runner.request_scan()  # no-op without a loop, must not raise


def test_refresh_now_updates_snapshot_without_scanning_status(tmp_path):
    """refresh_now patches the snapshot from a synchronous cache-warm scan and
    never flips to 'scanning' — so a single-album mutation can reflect in one
    render with no async rescan (and no inbox busy-lock flash). See #11."""
    runner = ScanRunner(tmp_path)
    _album(tmp_path, "One")
    runner.refresh_now()
    assert {a.path.name for a in runner.albums()} == {"One"}
    assert runner.has_completed() is True
    assert runner.status()["state"] == "idle"  # never advertised a scan

    # A subsequent change shows up on the next refresh.
    _album(tmp_path, "Two")
    runner.refresh_now()
    assert {a.path.name for a in runner.albums()} == {"One", "Two"}


def test_request_scan_after_loop_close_is_noop(tmp_path):
    """A sync/reconcile thread can outlive the app's event loop (daemon thread,
    lifespan already torn down) and still call request_scan() as its last act.
    That must be a silent no-op, not 'RuntimeError: Event loop is closed'
    surfacing as a bogus 'reconcile run failed' (issue #52)."""
    runner = ScanRunner(tmp_path)

    async def go() -> None:
        runner.attach_loop()
        await _wait(runner.has_completed)

    asyncio.run(go())  # asyncio.run closes the loop on exit
    assert runner.is_engaged()  # still holds the (now closed) loop
    runner.request_scan()  # must not raise
    runner.reset_and_rescan()  # must not raise, must not clear the snapshot


def test_reset_and_rescan_no_loop_is_noop(tmp_path):
    """Before the runner is engaged, reset_and_rescan must be a safe no-op (the
    erase-sidecars handler may run before/without the background loop)."""
    runner = ScanRunner(tmp_path)
    runner._albums = ["stale"]  # type: ignore[list-item]
    runner.reset_and_rescan()  # no loop → must not raise, must not clear
    assert len(runner.albums()) == 1


def test_reset_and_rescan_drops_snapshot_and_reruns(tmp_path):
    """After a nuke: reset_and_rescan clears the stale snapshot and runs a fresh
    scan, so the inbox shows 'Scanning…' then the rebuilt library."""
    music = tmp_path / "music"
    _album(music, "A")
    _album(music, "B")
    runner = ScanRunner(music)

    async def go() -> None:
        runner.attach_loop()
        await _wait(runner.has_completed)
        assert len(runner.albums()) == 2
        first = runner.status()["seq"]
        runner.reset_and_rescan()
        # The snapshot is dropped promptly (the rescan then refills it).
        await _wait(lambda: runner.albums() == [])
        await _wait(lambda: runner.status()["seq"] > first)

    asyncio.run(go())
    assert len(runner.albums()) == 2  # repopulated by the fresh scan


def test_scan_now_is_synchronous_and_cache_backed(tmp_path):
    """scan_now() works off the loop (the sync runner's post-sync matching uses
    it) and populates/reuses the mtime cache so repeat scans are cheap."""
    music = tmp_path / "music"
    _album(music, "A")
    _album(music, "B")
    runner = ScanRunner(music)
    albums = runner.scan_now()  # no attach_loop needed
    assert {a.path.name for a in albums} == {"A", "B"}
    assert runner.cache_size() == 2  # cache populated
    assert len(runner.scan_now()) == 2  # second call (cache hits) still correct


def test_scan_runner_scans_and_reports(tmp_path):
    music = tmp_path / "music"
    _album(music, "A")
    _album(music, "B")
    runner = ScanRunner(music)

    async def go() -> None:
        runner.attach_loop()  # captures loop + kicks the initial scan
        await _wait(runner.has_completed)

    asyncio.run(go())

    assert runner.is_engaged()
    assert runner.has_completed()
    assert len(runner.albums()) == 2
    status = runner.status()
    assert status["state"] == "done"
    assert status["albums_found"] == 2
    assert status["dirs_scanned"] >= 2


def test_scan_runner_seq_increments_each_completed_scan(tmp_path):
    """The completed-scan counter advances on every scan — the signal the
    client uses to refresh even when a scan is too fast to observe mid-flight."""
    music = tmp_path / "music"
    _album(music, "A")
    runner = ScanRunner(music)

    async def go() -> None:
        runner.attach_loop()
        await _wait(runner.has_completed)
        first = runner.status()["seq"]
        assert first >= 1
        runner.request_scan()  # even with no disk change, a scan still completes
        await _wait(lambda: runner.status()["seq"] > first)

    asyncio.run(go())
    assert runner.status()["seq"] >= 2


def test_scan_runner_fires_on_first_complete_once(tmp_path):
    """The on-first-complete hook (used to kick reconcile on startup) fires
    exactly once — after the first scan, not on subsequent rescans."""
    music = tmp_path / "music"
    _album(music, "A")
    runner = ScanRunner(music)
    calls: list[int] = []
    runner.set_on_first_complete(lambda: calls.append(1))

    async def go() -> None:
        runner.attach_loop()
        await _wait(runner.has_completed)
        first = runner.status()["seq"]
        runner.request_scan()  # a second scan must NOT re-fire the hook
        await _wait(lambda: runner.status()["seq"] > first)
        await asyncio.sleep(0.02)

    asyncio.run(go())
    assert calls == [1]


def test_scan_runner_rescan_picks_up_new_album(tmp_path):
    music = tmp_path / "music"
    _album(music, "A")
    runner = ScanRunner(music)

    async def go() -> None:
        runner.attach_loop()
        await _wait(runner.has_completed)
        assert len(runner.albums()) == 1
        # New album appears on disk; a requested re-scan must pick it up.
        _album(music, "B")
        runner.request_scan()
        await _wait(lambda: len(runner.albums()) == 2)

    asyncio.run(go())
    assert {a.path.name for a in runner.albums()} == {"A", "B"}


# ---------- Discovery records (#107) ----------


def _discovery_rows() -> list:
    from harmonist import activity_store
    from harmonist.activity_store import Source

    return [
        e
        for e in activity_store.recent(200, source=Source.AUDIT)
        if e.message.startswith(activity_store.DISCOVERY_EVENT)
    ]


def test_untouched_albums_are_recorded_without_claiming_when_they_arrived(tmp_path):
    """A sidecar-less album might have turned up ten minutes ago or ten years
    ago, and Harmonist cannot tell which. So the entry says what it actually
    knows — when it started keeping records — and nothing about arrival (#116)."""
    from harmonist import activity, activity_store, id_registry

    music = tmp_path / "music"
    _album(music, "A")
    _album(music, "B")
    activity_store.init(tmp_path / "activity.db")
    activity_store.clear()
    id_registry.set_library_root(music)
    runner = ScanRunner(music)

    async def go() -> None:
        runner.attach_loop()
        await _wait(runner.has_completed)
        await _wait(lambda: len(_discovery_rows()) == 2)

    asyncio.run(go())

    # Exact, not a substring: the old wording ("…already in your library") also
    # contained this, and the whole point is that the sentence stops here.
    entries = [e.message for e in activity.recent(20)]
    assert "Started tracking 2 albums" in entries, entries


def test_an_album_with_a_sidecar_is_never_recorded_as_discovered(tmp_path):
    """A sidecar means Harmonist has written to this album before, so it already
    HAS history. Recording a discovery anyway put an `album.discovered` row above
    the album's own download and tagging rows, dated later (#116)."""
    from harmonist import activity, activity_store, id_registry
    from harmonist import sidecar as scmod
    from harmonist.models import Sidecar

    music = tmp_path / "music"
    known = _album(music, "AlreadyKnown")
    _album(music, "Untouched")
    activity_store.init(tmp_path / "activity.db")
    id_registry.set_library_root(music)
    scmod.write(known, Sidecar(mb_release_id="rel-known"))
    activity_store.clear()  # forget the sidecar.create the write just audited
    runner = ScanRunner(music)

    async def go() -> None:
        runner.attach_loop()
        await _wait(runner.has_completed)
        await _wait(lambda: len(_discovery_rows()) == 1)
        await asyncio.sleep(0.05)  # give a wrong second row time to appear

    asyncio.run(go())

    rows = _discovery_rows()
    assert len(rows) == 1, [r.message for r in rows]
    assert "Untouched" in rows[0].message
    assert not any("AlreadyKnown" in r.message for r in rows)
    entries = [e.message for e in activity.recent(20)]
    assert any("Started tracking 1 album" in m for m in entries), entries


def test_an_album_added_later_is_recorded_once_and_only_once(tmp_path):
    from harmonist import activity_store, id_registry

    music = tmp_path / "music"
    _album(music, "Existing")
    activity_store.init(tmp_path / "activity.db")
    activity_store.clear()
    id_registry.set_library_root(music)
    runner = ScanRunner(music)

    async def go() -> None:
        runner.attach_loop()
        await _wait(lambda: len(_discovery_rows()) == 1)
        _album(music, "Arrived")
        runner.request_scan()
        await _wait(lambda: len(_discovery_rows()) == 2)
        # A third scan with nothing new must add nothing — the scan runs
        # constantly, so a per-scan repeat would bury the feed.
        runner.request_scan()
        await _wait(lambda: runner.status()["seq"] >= 3)
        await asyncio.sleep(0.05)

    asyncio.run(go())

    assert len(_discovery_rows()) == 2


def test_a_discovered_album_reaches_its_own_history_page(tmp_path):
    """The point of the record: for an ADOPTED album — no download, no sidecar —
    this is the only thing that answers "where did this come from?"."""
    from harmonist import activity_store, id_registry

    music = tmp_path / "music"
    _album(music, "Adopted")
    activity_store.init(tmp_path / "activity.db")
    activity_store.clear()
    id_registry.set_library_root(music)
    runner = ScanRunner(music)

    async def go() -> None:
        runner.attach_loop()
        await _wait(lambda: len(_discovery_rows()) == 1)

    asyncio.run(go())

    album = runner.albums()[0]
    assert not (album.path / ".harmonist.json").exists()  # no sidecar: id is path-derived
    history = [e.message for e in activity_store.album_history(album.id)]
    assert any(m.startswith(activity_store.DISCOVERY_EVENT) for m in history), history


def test_the_discovery_entry_carries_its_albums_as_what_changed(tmp_path):
    """One activity entry per scan, audit rows inside its action scope — so the
    feed says "a scan found N" while each row lands on its own album's page."""
    from harmonist import activity, activity_store, id_registry

    music = tmp_path / "music"
    _album(music, "A")
    _album(music, "B")
    activity_store.init(tmp_path / "activity.db")
    activity_store.clear()
    id_registry.set_library_root(music)
    runner = ScanRunner(music)

    async def go() -> None:
        runner.attach_loop()
        await _wait(lambda: len(_discovery_rows()) == 2)

    asyncio.run(go())

    entry = next(e for e in activity.recent(20) if "Started tracking" in e.message)
    assert entry.action_id, "discovery entry ran outside an action scope"
    detail = activity_store.audit_by_action([entry.action_id])[entry.action_id]
    assert len(detail) == 2  # one line per album, not one entry per album
    assert all(r.message.startswith(activity_store.DISCOVERY_EVENT) for r in detail)
