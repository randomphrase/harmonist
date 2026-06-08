"""Asyncio background library scanner.

Walks the music dir off the request path and keeps an in-memory snapshot of
Albums that the web routes serve instantly — so a cold scan of a large library
over a slow filesystem never blocks `GET /` or `/tasks`.

Single event loop, NO threads. Filesystem I/O (mutagen tag reads, `stat`) is
inherently blocking — there is no true async fs walk in Python — so the scan
yields cooperatively (`await asyncio.sleep(0)`) every ~50ms of work, letting
request handlers interleave between reads. The one residual cost is that a
single pathologically slow read briefly stalls the loop; that's the trade for
not using a thread.

The runner is "engaged" once `attach_loop()` runs (from the FastAPI lifespan).
Until then — e.g. in unit tests that build a TestClient without the lifespan —
callers fall back to a synchronous `scanner.scan()`, preserving the old
request-time behaviour.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harmonist import scanner
from harmonist.models import Album

log = logging.getLogger(__name__)

# Cooperative yield cadence: hand the event loop back at least this often
# (seconds of wall-clock work) so requests don't wait behind the whole scan.
_YIELD_INTERVAL_S = 0.05


@dataclass
class ScanStatus:
    state: str = "idle"  # "idle" | "scanning" | "done"
    dirs_scanned: int = 0
    albums_found: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "dirs_scanned": self.dirs_scanned,
            "albums_found": self.albums_found,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "last_error": self.last_error,
        }


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class ScanRunner:
    """Owns the background scan task + the album snapshot it produces."""

    def __init__(self, music_dir: Path) -> None:
        self._music_dir = music_dir
        self._cache: scanner.AlbumCache = {}
        self._albums: list[Album] = []
        self._completed_once = False
        self._status = ScanStatus()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._dirty = True  # a (re)scan is wanted

    # ----- engagement (lifespan) -----

    def attach_loop(self) -> None:
        """Capture the running loop and kick the initial scan. Call from the
        lifespan startup (inside the event loop)."""
        self._loop = asyncio.get_running_loop()
        self._kick()

    def is_engaged(self) -> bool:
        """True once the background runner is driving scans (lifespan ran).
        When False, callers should scan synchronously themselves."""
        return self._loop is not None

    # ----- reads (called from request handlers / threadpool) -----

    def albums(self) -> list[Album]:
        """The most recent snapshot. Empty until the first scan completes.
        Reference read is atomic under the GIL — safe across threads."""
        return self._albums

    def has_completed(self) -> bool:
        return self._completed_once

    def status(self) -> dict[str, Any]:
        return self._status.to_dict()

    # ----- triggers -----

    def request_scan(self) -> None:
        """Mark the library dirty and ensure a scan runs. Thread-safe — safe to
        call from FastAPI's sync route handlers (threadpool) and the sync/
        reconcile runner threads. No-op until engaged."""
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._kick)

    def _kick(self) -> None:
        # Always runs in the event loop thread.
        self._dirty = True
        if self._task is None or self._task.done():
            assert self._loop is not None
            self._task = self._loop.create_task(self._run())

    # ----- the scan itself -----

    async def _run(self) -> None:
        # Coalesce: if more changes land while scanning, scan again after.
        while self._dirty:
            self._dirty = False
            try:
                await self._scan_once()
            except Exception as e:  # never let the task die silently
                log.exception("library scan failed")
                self._status.state = "idle"
                self._status.last_error = str(e)

    async def _scan_once(self) -> None:
        status = ScanStatus(state="scanning", started_at=datetime.now(UTC))
        self._status = status
        results: list[Album] = []
        last_yield = time.monotonic()
        for album_dir, files, sig in scanner.iter_album_dirs(self._music_dir):
            status.dirs_scanned += 1
            album = scanner.resolve_album(album_dir, files, sig, self._cache)
            if album is not None:
                results.append(album)
                status.albums_found = len(results)
            if time.monotonic() - last_yield >= _YIELD_INTERVAL_S:
                await asyncio.sleep(0)
                last_yield = time.monotonic()
        scanner.prune_cache(self._cache, {a.path for a in results})
        self._albums = results
        self._completed_once = True
        status.state = "done"
        status.finished_at = datetime.now(UTC)
