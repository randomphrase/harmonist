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

from harmonist import activity, activity_store, audit, library_index, live_counts, scanner
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

    def refresh_now(self) -> None:
        """Synchronously refresh the snapshot from a cache-warm scan and patch
        `_albums` in place, WITHOUT flipping status to 'scanning'. A single-album
        mutation that must show immediately pairs this with
        `request.state.skip_rescan = True`, so the album resolves in one render
        instead of via the async post-mutation rescan — whose 'scanning' status
        dims the inbox (the #11 flicker). Safe from a worker thread: the cache is
        shared safely (see `scan_now`) and the `_albums` assignment is GIL-atomic."""
        self._albums = self.scan_now()
        self._completed_once = True

    def status(self) -> dict[str, Any]:
        return self._status.to_dict()

    # ----- triggers -----

    def request_scan(self) -> None:
        """Mark the library dirty and ensure a scan runs. Thread-safe — safe to
        call from FastAPI's sync route handlers (threadpool) and the sync/
        reconcile runner threads. No-op until engaged, and again after the app
        shuts down — a sync/reconcile thread finishing its last pass during
        teardown must not die with 'Event loop is closed' (issue #52)."""
        self._kick_threadsafe(self._kick)

    def reset_and_rescan(self) -> None:
        """Drop the current snapshot, then kick a fresh scan. Used after a nuke
        (erase-sidecars): the old snapshot is now stale, so clear it so the inbox
        shows the 'Scanning…' screen straight away instead of lingering on the
        now-wrong cards until the rescan lands. Thread-safe; no-op until engaged.

        (The rescan itself is cheap now — erasing sidecars leaves the audio
        unchanged, so the cache reuses the tag fields and only re-reads the absent
        sidecars.)"""
        self._kick_threadsafe(self._reset_and_kick)

    def _kick_threadsafe(self, kick: Callable[[], None]) -> None:
        # No-op when not yet engaged OR the loop has since closed (app shutdown).
        # The is_closed() check races with close(), so the call itself can still
        # raise — swallow that too; a scan requested during teardown is moot.
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(kick)
        except RuntimeError:
            pass

    def _reset_and_kick(self) -> None:
        # On the loop thread: clear the snapshot before the scan task starts so a
        # /tasks render in between sees an empty inbox + (imminent) scanning.
        self._albums = []
        self._kick()

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
                    album = cached[1]  # full-signature hit → zero I/O
                else:
                    # Audio unchanged (only the sidecar/cover moved)? Reuse the
                    # cached tag fields so the worker skips the per-track mutagen
                    # reads (the expensive part) and re-reads only the cheap
                    # sidecar + cover. This is what keeps a post-sync/reconcile
                    # rescan fast even though every linked album's sidecar changed.
                    reuse = None
                    if cached is not None and cached[0][0] == signature[0]:
                        reuse = cached[2]  # audio unchanged → skip the tag reads
                    io = await loop.run_in_executor(
                        executor, scanner.read_album_io, album_dir, files, reuse
                    )
                    album = scanner.build_album(album_dir, files, io)  # CPU, on loop
                    self._cache[album_dir] = (signature, album, io.fields)
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
        self._announce_discoveries(results)
        # Reset the authoritative live counts from this fresh snapshot — the
        # self-healing baseline that transitions (live_counts.move) adjust between
        # scans. This snapshot IS what the UI reads, so the counts now match it.
        live_counts.reset_from(results)
        # Same self-healing baseline for the sidecar/dedup index — rebuilt from the
        # fresh snapshot, then kept current by sidecar writes between scans.
        library_index.reset_from(results)
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

    def _announce_discoveries(self, albums: list[Album]) -> None:
        """Record albums Harmonist has never touched before (#107).

        ONE activity entry per scan, with an audit row per album inside its action
        scope. That gives both readings from one write: the feed says "started
        tracking 12", and each row's `album_id` puts it on that album's own
        history page — answering "where did this album even come from?", which for
        an ADOPTED album nothing else could.

        One entry per scan rather than per album is also what keeps the volume
        honest: dropping a large collection in adds a line to the feed, not
        thousands.

        The audit rows ARE the ledger — an album has been met iff a row says so —
        which is only possible because a sidecar-less album's id now derives from
        its path (#114) and so survives a restart.

        The wording deliberately claims nothing about WHEN an album arrived. A
        sidecar-less album might have turned up ten minutes ago or ten years ago,
        and Harmonist cannot tell which — all it knows is when it started keeping
        records for it (#116).

        Bookkeeping about a scan, so it must never fail one — hence the broad
        catch. Loud, though: silence here would mean albums quietly never getting
        the record, which is the bug this fixes.
        """
        try:
            # A sidecar means Harmonist has written to this album before, so it
            # already HAS history and needs no start marker. Recording one anyway
            # put an `album.discovered` row above an album's own download and
            # tagging rows, dated a day later — true, useless, and misleading
            # (#116). This leaves the record doing only the job it exists for:
            # the adopted album nothing else can account for.
            candidates = [a for a in albums if a.sidecar is None]
            known = activity_store.already_discovered([a.id for a in candidates])
            fresh = [a for a in candidates if a.id not in known]
            if not fresh:
                return
            with activity_store.action():
                activity.info(
                    f"Started tracking {len(fresh)} album{'s' if len(fresh) != 1 else ''}"
                )
                for a in fresh:
                    # `album` (the path) rather than only the id, so the row still
                    # names which album it was about if that album is later moved
                    # and re-identified.
                    audit.record(
                        activity_store.DISCOVERY_EVENT,
                        album_id=a.id,
                        album=a.path,
                        state=a.state,
                        tracks=a.track_count,
                    )
        except Exception:
            log.exception("could not record newly-discovered albums")
