"""Asyncio background library scanner.

Walks the music dir off the request path and keeps an in-memory snapshot of
Albums that the web routes serve instantly — so a cold scan of a large library
over a slow filesystem never blocks `GET /` or `/tasks`.

Filesystem I/O (mutagen tag reads, `stat`) is inherently blocking — there is no
true async fs walk in Python — so it runs on a single dedicated worker thread
and the loop awaits it one directory at a time. Request handlers interleave
freely between those awaits, and no read, however slow, stalls the loop.

Each directory is resolved by `scanner.resolve_dir` — the SAME function the
synchronous `scanner.scan` uses. That is deliberate and load-bearing: this
module used to inline its body, the two drifted, and the background scan quietly
stopped stamping the timestamp external-re-tag detection is built on (#230).

The runner is "engaged" once `attach_loop()` runs (from the FastAPI lifespan).
Until then — e.g. in unit tests that build a TestClient without the lifespan —
callers fall back to a synchronous `scanner.scan()`, preserving the old
request-time behaviour.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Sequence
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
    # Monotonic count of scans that produced a DIFFERENT snapshot. The client
    # watches this to know a fresh snapshot exists and refresh the inbox —
    # robust even when a scan is so fast (mtime-cache hit) that it starts AND
    # finishes between two status polls, which the old scanning→done state edge
    # would miss.
    #
    # Different, not merely completed: since #151 the library is rescanned
    # hourly whether or not anything happened, and a counter that ticked on
    # every one of those would re-render the inbox every hour over an
    # unchanged library.
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
        # The per-DIRECTORY entries `_albums` was folded from. Retained because
        # `merge_by_identity` is one-way: a merged Album no longer knows which of
        # its parts came from which directory, so refreshing ONE album (#151)
        # would otherwise have to re-walk the whole library to rebuild the rest.
        self._scanned: list[scanner.ScannedDir] = []
        # Guards the read-modify-write of that pair. Everything else on the
        # runner is either loop-thread-only or a single GIL-atomic assignment,
        # but `refresh_album` reads `_scanned`, rebuilds it and publishes both —
        # from FastAPI's threadpool, while a background scan may be about to
        # publish its own. Held only for pure-CPU work over data already read.
        self._snapshot_lock = threading.Lock()
        self._completed_once = False
        self._scan_seq = 0  # monotonic; stamped onto status at each completion
        self._status = ScanStatus()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._dirty = True  # a (re)scan is wanted
        # Whether the wanted scan should ADVERTISE itself. A scan somebody asked
        # for — a mutation, the watcher, startup — dims and locks the inbox while
        # it runs, so a click can't land on a snapshot being rebuilt. The
        # hourly rescan (#151) asked nobody, and locking the inbox on its own
        # schedule, possibly mid-click, is not something it has earned. So it runs
        # invisibly: same scan, no status.
        self._visible = True  # the startup scan is very much visible
        # The in-flight scan's status object, so a real trigger arriving during
        # a quiet rescan can promote it mid-flight (see `_kick`).
        self._current: ScanStatus | None = None
        # Fired once, after the FIRST scan completes — used to kick reconcile
        # backend-side so it runs without waiting for the frontend /tasks poll.
        self._on_first_complete: Callable[[], object] | None = None
        # A SINGLE worker thread runs all the scan's blocking filesystem I/O
        # (walk/stat + tag reads) AND the album build that follows it, so the
        # event loop never blocks on syscalls. One worker keeps reads serial →
        # no parallel-I/O concurrency, and the loop awaits each hand-off, so the
        # two threads are never inside the resolver at once.
        #
        # The mtime cache is the one piece of mutable state the worker touches:
        # `scanner.resolve_dir` reads and writes it there. That is the same
        # sharing `scan_now` already relies on from FastAPI's threadpool — a
        # dict, GIL-atomic per entry, worst case a redundant re-read of one
        # album. The snapshot and the status stay loop-thread-only.
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
        shared safely (see `scan_now`) and the `_albums` assignment is GIL-atomic.

        Deliberately does NOT reset `live_counts` / `library_index` — a caller
        that moved an album between states has already told `live_counts` about
        it, and the full rescan is what re-derives them. This only patches the
        snapshot."""
        self._publish(scanner.scan_dirs(self._music_dir, album_cache=self._cache))
        self._completed_once = True

    def refresh_album(self, dirs: Sequence[Path]) -> None:
        """Re-read just these directories from disk and patch the snapshot (#151).

        The album page's guarantee, made unconditional: what you are looking at
        is what is on disk right now, whatever the watcher did or didn't see.
        That matters most exactly there, because the album page is where the
        user decides whether to re-tag — and deciding that against a stale
        reading is the failure that costs something.

        Cost is one directory listing plus a `stat` per file. On a signature hit
        that is the whole of it: `resolve_dir` returns the cached Album and reads
        no tags. `refresh_now` is NOT an alternative here — it walks the entire
        library, which is cheap after a mutation and not cheap per page view on
        a NAS, which is the case this exists for.

        Blocking I/O, so call it from a worker thread (a sync route handler),
        never the event loop. Only the directories named are re-read: an album
        gaining a WHOLE NEW directory (a second disc dropped in elsewhere) is
        beyond what one album's paths can find, and stays the hourly rescan's job.

        A directory that can no longer be read is left as it was — a network
        mount blinking must not delete albums from the snapshot. One that reads
        fine but no longer resolves (its audio is gone, its sidecar is corrupt)
        is dropped, which is what a full scan would have concluded about it too.
        """
        known = {e.album.path for e in self._scanned}
        if not known:
            return  # nothing to patch: no scan has produced a snapshot yet
        fresh: dict[Path, scanner.ScannedDir | None] = {}
        for d in dirs:
            if d not in known:
                # Only ever REPLACES entries the last scan produced. Reading a
                # directory the snapshot has never heard of would put it in the
                # mtime cache without ever putting it in the snapshot.
                continue
            try:
                contents = scanner.read_dir(d)
            except OSError as e:
                # Debug, not warning: WARNING+ is mirrored into the Activity
                # feed, and this fires once per album-page view — a flaky mount
                # would fill the feed with a condition that costs the user
                # nothing, since the entry it couldn't refresh is kept and the
                # page renders. The hourly rescan is what reports a real problem.
                log.debug("could not re-read %s: %s", d, e)
                continue  # leave the existing entry alone
            if contents is None:
                fresh[d] = None  # readable, but no audio left in it
                continue
            fresh[d] = scanner.resolve_dir(
                d, contents.files, contents.videos, contents.signature, self._cache
            )
        if not fresh:
            return
        with self._snapshot_lock:
            rebuilt: list[scanner.ScannedDir] = []
            for entry in self._scanned:
                if entry.album.path not in fresh:
                    rebuilt.append(entry)
                    continue
                replacement = fresh[entry.album.path]
                if replacement is not None:
                    rebuilt.append(replacement)
                else:
                    self._cache.pop(entry.album.path, None)
            self._publish_locked(rebuilt)

    def _publish(self, scanned: list[scanner.ScannedDir]) -> tuple[list[Album], bool]:
        with self._snapshot_lock:
            return self._publish_locked(scanned)

    def _publish_locked(self, scanned: list[scanner.ScannedDir]) -> tuple[list[Album], bool]:
        """Fold the per-directory entries into albums and publish both, so the
        two never disagree. Caller holds `_snapshot_lock`. Also answers whether
        the snapshot actually CHANGED, which is what the client's refresh hangs
        off since the hourly rescan started producing identical ones.

        The comparison is cheap in the case that matters: an unchanged directory
        is served from the mtime cache as the very same `Album` object, and list
        equality short-circuits on identity per element."""
        results = scanner.merge_by_identity(scanned)
        changed = results != self._albums
        self._scanned = scanned
        self._albums = results
        return results, changed

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

    def request_quiet_rescan(self) -> None:
        """Like `request_scan`, but for a rescan NOBODY asked for — the hourly
        one (#151).

        Identical work, deliberately without the status: the inbox busy-lock
        exists so a click can't land on a snapshot being rebuilt by something
        the user set in motion, and applying it hourly on a timer would dim and
        freeze the inbox under someone's cursor for no reason they could see.
        A backstop for a watcher that isn't working should be invisible when the
        watcher is.

        A real trigger arriving mid-rescan promotes the scan in flight, so the
        lock appears exactly when there IS something to protect. Thread-safe;
        no-op until engaged."""
        self._kick_threadsafe(self._kick_quiet)

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
        # The retained per-directory entries go with it — leaving them would let
        # a per-album refresh in the gap republish the snapshot just cleared.
        with self._snapshot_lock:
            self._scanned = []
            self._albums = []
        self._kick()

    def _kick(self) -> None:
        # Always runs in the event loop thread.
        self._dirty = True
        self._visible = True
        # A real trigger during a quiet rescan promotes the scan in flight,
        # rather than leaving it invisible until the next pass: from here on
        # there IS something the inbox lock is protecting.
        if self._current is not None and self._status is not self._current:
            self._current.state = "scanning"
            self._status = self._current
        self._start()

    def _kick_quiet(self) -> None:
        # A quiet rescan: wanted, but it must not claim the status if nothing else has.
        self._dirty = True
        self._start()

    def _start(self) -> None:
        if self._task is None or self._task.done():
            assert self._loop is not None
            self._task = self._loop.create_task(self._run())

    # ----- the scan itself -----

    async def _run(self) -> None:
        # Coalesce: if more changes land while scanning, scan again after.
        while self._dirty:
            self._dirty = False
            # Claim the visibility this pass was asked for, so a trigger landing
            # DURING it makes the next pass visible rather than being lost.
            visible = self._visible
            self._visible = False
            try:
                await self._scan_once(visible=visible)
            except Exception as e:  # never let the task die silently
                log.exception("library scan failed")
                self._status.state = "idle"
                self._status.last_error = str(e)
            finally:
                self._current = None

    async def _scan_once(self, *, visible: bool) -> None:
        loop = asyncio.get_running_loop()
        executor = self._executor
        assert executor is not None  # set in attach_loop before any scan

        started = time.monotonic()
        log.info("Library %s started: %s", "scan" if visible else "rescan", self._music_dir)
        # Carry the last completed seq while scanning; bump it only on completion
        # so the client refreshes when a NEW snapshot is actually ready.
        status = ScanStatus(state="scanning", started_at=datetime.now(UTC), seq=self._scan_seq)
        # An invisible rescan still builds a status — the progress counters below
        # are written unconditionally — it just never publishes it. `_current`
        # is how `_kick` reaches in and promotes this scan if a real trigger
        # arrives while it runs.
        self._current = status
        if visible:
            self._status = status
        # One entry per DIRECTORY. Identity grouping (#197) happens once at the
        # end, over data already read — the walk and the cache stay per-directory
        # so that costs no extra I/O, and progress still counts directories,
        # which is what the user watches move.
        scanned: list[scanner.ScannedDir] = []
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
            album_dir, files, videos, signature = cast(
                "tuple[Path, list[Path], list[Path], scanner.AlbumSignature]", item
            )
            status.dirs_scanned += 1
            # The SAME resolver the synchronous scan uses — cache lookup, tag
            # reuse and build in one call, on the worker thread. Inlining its
            # body here instead let the two drift, and the background scan
            # silently stopped stamping `files_written_at`, which is the whole
            # of external-re-tag detection (#230). One resolver, no drift.
            # It swallows a bad album itself (logging it) and returns None, so
            # one unreadable directory still can't abort the scan.
            entry = await loop.run_in_executor(
                executor, scanner.resolve_dir, album_dir, files, videos, signature, self._cache
            )
            if entry is not None:
                scanned.append(entry)
                status.albums_found = len(scanned)

            now = time.monotonic()
            if now - last_log >= _LOG_INTERVAL_S:
                log.info(
                    "Library scan in progress: %d dirs, %d albums (%.0fs)",
                    status.dirs_scanned,
                    status.albums_found,
                    now - started,
                )
                last_log = now

        scanner.prune_cache(self._cache, {e.album.path for e in scanned})
        # Fold the directories that hold parts of one release into single albums.
        # Pure CPU over tags already read, so it stays on the loop thread.
        # Under the lock so it can't interleave with a per-album refresh
        # rebuilding the same pair from the threadpool (#151).
        results, changed = self._publish(scanned)
        status.albums_found = len(results)
        self._announce_discoveries(results)
        # Reset the authoritative live counts from this fresh snapshot — the
        # self-healing baseline that transitions (live_counts.move) adjust between
        # scans. This snapshot IS what the UI reads, so the counts now match it.
        live_counts.reset_from(results)
        # Same self-healing baseline for the sidecar/dedup index — rebuilt from the
        # fresh snapshot, then kept current by sidecar writes between scans.
        library_index.reset_from(results)
        first = not self._completed_once
        self._completed_once = True
        # Only a scan that produced a DIFFERENT snapshot advances the counter the
        # client refreshes off. An hourly rescan over an unchanged library must
        # leave the inbox alone entirely — it has nothing to say.
        if changed:
            self._scan_seq += 1
        status.seq = self._scan_seq
        status.state = "done"
        status.finished_at = datetime.now(UTC)
        if self._status is not status and changed:
            # An invisible rescan, never promoted: its status was never published,
            # and must not be now — but the client still has to learn a new
            # snapshot exists, or what the rescan found would sit unread until
            # something else happened to trigger a scan.
            self._status.seq = self._scan_seq
        log.info(
            "Library %s complete: %d albums across %d dirs in %.1fs%s",
            "scan" if visible else "rescan",
            len(results),
            status.dirs_scanned,
            time.monotonic() - started,
            "" if changed else " (no change)",
        )
        # Kick reconcile once the first snapshot is ready (backend-side, so it
        # runs even with no browser open). Only on the first scan — later scans
        # are covered by the /tasks kick, and re-firing here would churn.
        # Keyed on the FIRST completion rather than on the seq counter, which no
        # longer moves for a scan that found nothing: on an empty library that
        # would leave reconcile never kicked at all.
        if first and self._on_first_complete is not None:
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
