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
