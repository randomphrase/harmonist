"""Tests for the music-dir file watcher (web/dir_watcher.py)."""

from __future__ import annotations

import asyncio
from contextlib import suppress

from harmonist.web.dir_watcher import watch_music_dir


async def _wait(predicate, *, timeout_ticks: int = 400) -> None:
    for _ in range(timeout_ticks):
        if predicate():
            return
        await asyncio.sleep(0.01)


def test_watcher_rescans_after_change(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    calls: list[int] = []
    stop = asyncio.Event()

    async def go() -> None:
        task = asyncio.create_task(
            watch_music_dir(music, lambda: calls.append(1), settle_seconds=0.2, stop_event=stop)
        )
        await asyncio.sleep(0.3)  # let awatch start watching
        (music / "Artist").mkdir()
        (music / "Artist" / "track.flac").write_bytes(b"data")
        await _wait(lambda: bool(calls))
        stop.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert calls, "a change should trigger a rescan after the settle delay"


def test_watcher_coalesces_a_burst_into_one_rescan(tmp_path):
    """Several changes in quick succession settle into a single rescan."""
    music = tmp_path / "music"
    music.mkdir()
    calls: list[int] = []
    stop = asyncio.Event()

    async def go() -> None:
        task = asyncio.create_task(
            watch_music_dir(music, lambda: calls.append(1), settle_seconds=0.4, stop_event=stop)
        )
        await asyncio.sleep(0.3)
        # A burst of writes well within the settle window.
        for i in range(5):
            (music / f"track{i}.flac").write_bytes(b"data")
            await asyncio.sleep(0.03)
        await _wait(lambda: bool(calls))
        await asyncio.sleep(0.3)  # ensure no second (late) fire
        stop.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert calls == [1], f"a settled burst should rescan exactly once, got {len(calls)}"


def test_watcher_no_op_when_dir_missing(tmp_path):
    calls: list[int] = []

    async def go() -> None:
        # Returns immediately (not a directory) without scheduling anything.
        await watch_music_dir(
            tmp_path / "does-not-exist", lambda: calls.append(1), settle_seconds=0.1
        )

    asyncio.run(go())
    assert calls == []
