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

from harmonist import (
    activity,
    activity_store,
    artist_nfo,
    audit,
    contributions,
    formats,
    library_index,
    library_titles,
    live_counts,
    scanner,
)
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
        self._nfos = artist_nfo.Inventory()
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
        self._changed_paths: set[Path] = set()
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
        # Fired with the albums a publish rebuilt rather than kept (#758), so
        # the update flags they started without can be judged again.
        self._on_rebuilt: Callable[[list[Album]], object] | None = None
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

    def set_on_rebuilt(self, callback: Callable[[list[Album]], object]) -> None:
        """Register a callback fired with the albums each publish REBUILT (#758).

        Kept albums are the snapshot's own objects, carrying whatever has been
        worked out about them since they were read; a rebuilt one is new, and
        starts without it. That is the update flag's whole problem, and this is
        the list it needs: on an hourly rescan of an unchanged library it is
        empty, so nothing is read at all.

        Not fired by the first publish, where everything is new: the startup
        warm-up that scan triggers looks at the whole library anyway. Fired from
        whichever thread published, outside the snapshot lock, so it must return
        quickly."""
        self._on_rebuilt = callback

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

    def nfo_inventory(self) -> artist_nfo.Inventory:
        """Immutable local NFO evidence from the last completed scan."""
        return self._nfos

    def scanned_fields(self, album_dir: Path) -> list[formats.ScanFields] | None:
        """The tag fields the last scan read for one directory, in file order.

        None when the scan hasn't seen it. Lets a request ask about a folder's
        files (#876) without opening them again."""
        cached = self._cache.get(album_dir)
        return cached[2].fields if cached is not None else None

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
        discovery = artist_nfo.Discovery(self._music_dir)
        scanned = scanner.scan_dirs(self._music_dir, album_cache=self._cache, nfos=discovery)
        inventory = discovery.finish([entry.album for entry in scanned])
        with self._snapshot_lock:
            nfos_changed = inventory != self._nfos
            _, _, rebuilt = self._publish_locked(scanned, inventory)
            if nfos_changed:
                self._scan_seq += 1
                self._status.seq = self._scan_seq
        self._report_rebuilt(rebuilt)
        self._completed_once = True

    def refresh_album(self, dirs: Sequence[Path], *, notify: bool = False) -> bool:
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

        With `notify`, also publish counts, indexes and the refresh sequence
        after a mutation or watcher event. Return whether every directory could
        be refreshed; a false result lets mutation callers request a full scan.
        """
        known = {e.album.path for e in self._scanned}
        if not known:
            return False
        complete = True
        fresh: dict[Path, scanner.ScannedDir | None] = {}
        for d in dirs:
            if d not in known:
                # Only ever REPLACES entries the last scan produced. Reading a
                # directory the snapshot has never heard of would put it in the
                # mtime cache without ever putting it in the snapshot.
                complete = False
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
                if notify:
                    log.warning("Could not refresh %s; requesting a full scan: %s", d, e)
                complete = False
                continue  # leave the existing entry alone
            if contents is None:
                fresh[d] = None  # readable, but no audio left in it
                continue
            fresh[d] = scanner.resolve_dir(
                d, contents.files, contents.videos, contents.signature, self._cache
            )
        if not fresh:
            return complete
        with self._snapshot_lock:
            entries: list[scanner.ScannedDir] = []
            for entry in self._scanned:
                if entry.album.path not in fresh:
                    entries.append(entry)
                    continue
                replacement = fresh[entry.album.path]
                if replacement is not None:
                    entries.append(replacement)
                else:
                    self._cache.pop(entry.album.path, None)
            results, changed, rebuilt = self._publish_locked(entries)
            if notify:
                live_counts.reset_from(results)
                library_index.reset_from(results)
                if changed:
                    self._scan_seq += 1
                    self._status.seq = self._scan_seq
        self._report_rebuilt(rebuilt)
        return complete

    def _refresh_changed(self, paths: set[Path]) -> bool:
        """Refresh known file parents; uncertain directory changes need a walk."""
        nfo_paths = {p for p in paths if p.name.lower() == "artist.nfo"}
        if any(p.is_dir() for p in paths):
            return False  # New directory layout needs the collector's full walk.
        images_changed = any(p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} for p in paths)
        if (nfo_paths or images_changed) and self._completed_once and not self._nfos.walk_errors:
            self._refresh_nfos(nfo_paths)
            paths = paths - nfo_paths
            if not paths:
                return True
        known = {entry.album.path for entry in self._scanned}
        dirs = {path.parent for path in paths}
        if not self._completed_once or not dirs <= known:
            return False
        if any(path.is_dir() or path in known for path in paths):
            return False
        complete = self.refresh_album(sorted(dirs), notify=True)
        if complete and any(formats.is_supported(p) for p in paths):
            # Re-tagging can change whether a generic folder image belongs to
            # an artist. Reuse the new tag snapshot; do not reopen audio here.
            self._refresh_nfos(set())
        return complete

    def _refresh_nfos(self, paths: set[Path]) -> None:
        with self._snapshot_lock:
            baseline = self._nfos
            entries = tuple(self._scanned)
        discovery = artist_nfo.Discovery(
            self._music_dir,
            list(set(baseline.paths) | paths),
            album_dirs=set(baseline.album_dirs),
        )
        inventory = discovery.finish([entry.album for entry in entries])
        with self._snapshot_lock:
            if self._nfos is not baseline or tuple(self._scanned) != entries:
                # A newer publication wins. Retry against its album context
                # rather than putting an earlier folder classification back.
                self.request_quiet_rescan()
                return
            if inventory != self._nfos:
                self._nfos = inventory
                self._scan_seq += 1
                self._status.seq = self._scan_seq

    def _report_rebuilt(self, albums: list[Album]) -> None:
        # Before the first completion every album is new, and the warm-up covers it.
        if not albums or not self._completed_once or self._on_rebuilt is None:
            return
        try:
            self._on_rebuilt(albums)
        except Exception:
            # Bookkeeping after a publish that has already happened, so it must
            # not fail the scan or the mutation that published. Loud, though:
            # the visible symptom is only a Library filter missing albums.
            log.exception("on-rebuilt callback failed")

    def _publish_locked(
        self,
        scanned: list[scanner.ScannedDir],
        inventory: artist_nfo.Inventory | None = None,
    ) -> tuple[list[Album], bool, list[Album]]:
        """Fold the per-directory entries into albums and publish both, so the
        two never disagree. Caller holds `_snapshot_lock`. Also answers whether
        the snapshot actually CHANGED, which is what the client's refresh hangs
        off since the hourly rescan started producing identical ones, and which
        albums it rebuilt (`set_on_rebuilt`).

        The comparison is cheap in the case that matters: an unchanged directory
        is served from the mtime cache as the very same `Album` object, and list
        equality short-circuits on identity per element."""
        results, rebuilt = self._keep_unchanged(scanned, scanner.merge_by_identity(scanned))
        # Multi-folder albums are rebuilt from cached parts, whose observations
        # can predate a check on the combined album. Carry the freshest RELEASE
        # evidence; provenance and conclusions still belong to each local copy.
        observations: dict[str, contributions.Observation] = {}
        floor = datetime.min.replace(tzinfo=UTC)
        for album in [*self._albums, *results]:
            observed = album.contribution_observation
            if observed is None:
                continue
            previous = observations.get(observed.mbid)
            if previous is None or (observed.checked_at or floor) > (previous.checked_at or floor):
                observations[observed.mbid] = observed
        for album in results:
            mbid = album.sidecar.mb_release_id if album.sidecar else None
            album.contribution_observation = observations.get(mbid) if mbid else None
        changed = results != self._albums
        if inventory is not None:
            changed = changed or inventory != self._nfos
            self._nfos = inventory
        self._scanned = scanned
        self._albums = results
        library_titles.reset_from(results)
        return results, changed, rebuilt

    def _keep_unchanged(
        self, scanned: list[scanner.ScannedDir], merged: list[Album]
    ) -> tuple[list[Album], list[Album]]:
        """Swap back in every album the snapshot already holds unchanged; return
        the albums to publish and the ones that are new (#758).

        A single-folder album the cache served is already the snapshot's own
        object. An album assembled from folders is not — `merge_by_identity`
        builds two discs into one album, and two copies into albums with ids of
        their own, afresh every time — so whatever was worked out about it
        since it was read, its update flag above all, was lost on every rescan.
        When every folder came from the cache and the album still has the same
        id and folders, it IS the album already published: same files, same
        parts, same answer. Keeping that object is what a single-folder album
        gets for free, and a judgement still running on it lands where the
        Library reads it.

        Caller holds `_snapshot_lock`. Identity is compared through `id()`,
        which is safe here: every object compared is alive in `self._scanned`
        or `scanned` for the whole call."""
        kept_parts = {id(e.album) for e in self._scanned}
        parts = {e.album.path: e.album for e in scanned}
        published = {(a.id, a.folders): a for a in self._albums}
        results: list[Album] = []
        rebuilt: list[Album] = []
        for album in merged:
            before = published.get((album.id, album.folders))
            if before is album:
                results.append(album)
            elif before is not None and all(
                d in parts and id(parts[d]) in kept_parts for d in album.folders
            ):
                results.append(before)
            else:
                results.append(album)
                rebuilt.append(album)
        return results, rebuilt

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

    def request_refresh(self, paths: set[Path]) -> None:
        """Coalesce settled file events on the scan worker, off the event loop."""
        changed = paths.copy()

        def kick() -> None:
            self._changed_paths.update(changed)
            self._start()

        self._kick_threadsafe(kick)

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
            self._nfos = artist_nfo.Inventory()
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
        while self._dirty or self._changed_paths:
            full_scan = self._dirty
            paths = self._changed_paths
            self._changed_paths = set()
            self._dirty = False
            # Claim the visibility this pass was asked for, so a trigger landing
            # DURING it makes the next pass visible rather than being lost.
            visible = self._visible
            self._visible = False
            try:
                if full_scan:
                    await self._scan_once(visible=visible)
                else:
                    loop = asyncio.get_running_loop()
                    refreshed = await loop.run_in_executor(
                        self._executor, self._refresh_changed, paths
                    )
                    if not refreshed:
                        self._kick()
            except Exception as e:  # never let the task die silently
                log.exception("library scan failed")
                self._status.state = "idle"
                self._status.last_error = str(e)
                if not full_scan:
                    self._kick()
            finally:
                self._current = None

    async def _scan_once(self, *, visible: bool) -> None:
        loop = asyncio.get_running_loop()
        executor = self._executor
        assert executor is not None  # set in attach_loop before any scan

        started = time.monotonic()
        with self._snapshot_lock:
            baseline = {entry.album.path: entry for entry in self._scanned}
            baseline_nfos = self._nfos
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
        discovery = artist_nfo.Discovery(self._music_dir)
        walk = scanner.iter_album_dirs(self._music_dir, nfos=discovery)
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

        inventory = await loop.run_in_executor(
            executor, discovery.finish, [entry.album for entry in scanned]
        )
        # Fold the directories that hold parts of one release into single albums.
        # Pure CPU over tags already read, so it stays on the loop thread.
        # Under the lock so it can't interleave with a per-album refresh
        # rebuilding the same pair from the threadpool (#151).
        with self._snapshot_lock:
            current = {entry.album.path: entry for entry in self._scanned}
            collected = {entry.album.path: entry for entry in scanned}
            for path in baseline.keys() | current.keys():
                if current.get(path) is not baseline.get(path):
                    if path in current:
                        collected[path] = current[path]
                    else:
                        collected.pop(path, None)
            scanner.prune_cache(self._cache, set(collected))
            # A synchronous refresh may have published newer NFO evidence while
            # this full walk was running, just as with the albums above.
            if self._nfos is not baseline_nfos:
                inventory = self._nfos
            results, changed, rebuilt = self._publish_locked(list(collected.values()), inventory)
            live_counts.reset_from(results)
            library_index.reset_from(results)
            if changed:
                self._scan_seq += 1
        self._report_rebuilt(rebuilt)
        status.albums_found = len(results)
        self._announce_discoveries(results)
        first = not self._completed_once
        self._completed_once = True
        # Only a scan that produced a DIFFERENT snapshot advances the counter the
        # client refreshes off. An hourly rescan over an unchanged library must
        # leave the inbox alone entirely — it has nothing to say.
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
                if len(fresh) == 1:
                    # One album: name it in the album column, as every other
                    # line about one album does, rather than counting it (#672).
                    activity.info(
                        "Started tracking", album_id=fresh[0].id, album_label=fresh[0].label
                    )
                else:
                    activity.info(f"Started tracking {len(fresh)} albums")
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
