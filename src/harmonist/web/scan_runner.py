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
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from harmonist import scanner
from harmonist.models import Album

log = logging.getLogger(__name__)

# How often to log a progress line during a long scan (slow FS / big library).
_LOG_INTERVAL_S = 3.0
# Sentinel for "the walk generator is exhausted" (run via the executor).
_DONE = object()


@dataclass
class ScanStatus:
    state: str = "idle"  # "idle" | "scanning" | "done"
    dirs_scanned: int = 0
    albums_found: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_error: str | None = None
    # Monotonic count of completed scans. The client watches this to know a
    # fresh snapshot exists and refresh the inbox — robust even when a scan is
    # so fast (mtime-cache hit) that it starts AND finishes between two status
    # polls, which the old scanning→done state edge would miss.
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "dirs_scanned": self.dirs_scanned,
            "albums_found": self.albums_found,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "last_error": self.last_error,
            "seq": self.seq,
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
        self._scan_seq = 0  # monotonic; stamped onto status at each completion
        self._status = ScanStatus()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._dirty = True  # a (re)scan is wanted
        # Fired once, after the FIRST scan completes — used to kick reconcile
        # backend-side so it runs without waiting for the frontend /tasks poll.
        self._on_first_complete: Callable[[], object] | None = None
        # A SINGLE worker thread runs all the scan's blocking filesystem I/O
        # (walk/stat + tag reads), so the event loop never blocks on syscalls.
        # One worker keeps reads serial → no parallel-I/O concurrency, and the
        # worker functions are pure (no shared state), so the only hand-off is
        # arg-in/result-out via the executor's Future. All mutable state (cache,
        # snapshot, status, id registry) is touched only on the loop thread.
        self._executor: ThreadPoolExecutor | None = None

    # ----- engagement (lifespan) -----

    def attach_loop(self) -> None:
        """Capture the running loop and kick the initial scan. Call from the
        lifespan startup (inside the event loop)."""
        self._loop = asyncio.get_running_loop()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="harmonist-scan")
        self._kick()

    def set_on_first_complete(self, callback: Callable[[], object]) -> None:
        """Register a callback fired (on the loop thread) once the FIRST scan
        completes. Set before `attach_loop`. Used to kick reconcile so it runs
        on startup without the frontend `/tasks` poll."""
        self._on_first_complete = callback

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

    def cache_size(self) -> int:
        """Number of albums held in the mtime cache (one entry per album dir
        carrying its signature + built Album). Exposed for memory diagnostics."""
        return len(self._cache)

    def scan_now(self) -> list[Album]:
        """A synchronous, cache-backed scan for a worker thread (the sync
        runner's post-sync matching). Reuses the background scanner's mtime cache,
        so it's FAST even right after a sync — only the albums whose sidecar just
        changed re-read tags; the rest are cache hits. (A cold ``scanner.scan``
        re-reads every album's tags — ~80s on a large NAS library, twice, which
        is the post-sync hang we're killing.)

        The cache is a dict, GIL-atomic per entry, so sharing it with an in-flight
        background scan is safe — worst case a redundant re-read of one album."""
        return scanner.scan(self._music_dir, album_cache=self._cache)

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
        loop = asyncio.get_running_loop()
        executor = self._executor
        assert executor is not None  # set in attach_loop before any scan

        started = time.monotonic()
        log.info("Library scan started: %s", self._music_dir)
        # Carry the last completed seq while scanning; bump it only on completion
        # so the client refreshes when a NEW snapshot is actually ready.
        status = ScanStatus(state="scanning", started_at=datetime.now(UTC), seq=self._scan_seq)
        self._status = status
        results: list[Album] = []
        last_log = started

        # The walk generator does blocking scandir/stat; it is advanced ONLY on
        # the worker thread (one step at a time, awaited), never concurrently.
        walk = scanner.iter_album_dirs(self._music_dir)
        while True:
            # Blocking walk+stat for the next album → worker; loop stays free.
            item = await loop.run_in_executor(executor, next, walk, _DONE)
            if item is _DONE:
                break
            album_dir, files, signature = cast(
                "tuple[Path, list[Path], scanner.AlbumSignature]", item
            )
            status.dirs_scanned += 1
            try:
                cached = self._cache.get(album_dir)
                if cached is not None and cached[0] == signature:
                    album = cached[1]  # mtime-cache hit → no tag reads
                else:
                    # Blocking sidecar + tag reads → worker; loop stays free.
                    io = await loop.run_in_executor(
                        executor, scanner.read_album_io, album_dir, files
                    )
                    album = scanner.build_album(album_dir, files, io)  # CPU, on loop
                    self._cache[album_dir] = (signature, album)
                results.append(album)
                status.albums_found = len(results)
            except Exception as e:  # one bad album must not abort the scan
                log.warning("error scanning %s: %s", album_dir, e)

            now = time.monotonic()
            if now - last_log >= _LOG_INTERVAL_S:
                log.info(
                    "Library scan in progress: %d dirs, %d albums (%.0fs)",
                    status.dirs_scanned,
                    status.albums_found,
                    now - started,
                )
                last_log = now

        scanner.prune_cache(self._cache, {a.path for a in results})
        self._albums = results
        self._completed_once = True
        self._scan_seq += 1
        status.seq = self._scan_seq
        status.state = "done"
        status.finished_at = datetime.now(UTC)
        log.info(
            "Library scan complete: %d albums across %d dirs in %.1fs",
            len(results),
            status.dirs_scanned,
            time.monotonic() - started,
        )
        # Kick reconcile once the first snapshot is ready (backend-side, so it
        # runs even with no browser open). Only on the first scan — later scans
        # are covered by the /tasks kick, and re-firing here would churn.
        if self._scan_seq == 1 and self._on_first_complete is not None:
            try:
                self._on_first_complete()
            except Exception:
                log.exception("on-first-scan-complete callback failed")
