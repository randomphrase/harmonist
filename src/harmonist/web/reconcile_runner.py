"""Background runner for per-album reconciliation.

Mirror of `SyncRunner`'s shape: single-flight, thread-based, status pollable.
When the scanner sees Orphan albums, `/tasks` kicks this runner (subject to a
small debounce so back-to-back polls don't spawn redundant work).

The runner iterates orphans, calls `reconcile.reconcile_album()` for each,
rate-limits MB queries at ~1/sec to stay within MusicBrainz's published
limits.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# Seconds between MB lookups during reconciliation. MusicBrainz documents a
# 1 req/sec limit; we err on the safe side.
MB_RATE_LIMIT_SECONDS = 1.0

# Seconds we must wait after a run completes before kicking another. Prevents
# /tasks polls (every 1.5s during sync) from spinning up redundant runs when
# all the orphans are already non-reconcilable (no MBID atom).
RERUN_DEBOUNCE_SECONDS = 5.0


@dataclass
class ReconcileStatus:
    state: str = "idle"  # "idle" | "running"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    current_item: str = ""
    completed: int = 0
    total: int = 0
    last_error: str | None = None
    # Live inbox/library counts DURING a pass: the already-sidecar'd base
    # (captured at start, excluding the orphans being reconciled) plus the
    # running outcome tallies. An un-reconciled orphan isn't in any count yet;
    # reconcile is what files it into one. Lets the UI show the counts move
    # without a mid-pass rescan. Only meaningful while state == "running".
    inbox: int = 0
    library: int = 0
    new: int = 0
    needs_sync: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "current_item": self.current_item,
            "completed": self.completed,
            "total": self.total,
            "last_error": self.last_error,
            "inbox": self.inbox,
            "library": self.library,
            "new": self.new,
            "needs_sync": self.needs_sync,
        }


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class ReconcileRunner:
    """Owns a single background reconciliation job at a time."""

    def __init__(self, runner_fn: Callable[..., None]):
        """`runner_fn(status_updater)` is the callable that iterates orphans
        and reconciles them. The runner injects a status updater so the
        function can report progress (current_item, completed counters).
        """
        self._runner_fn = runner_fn
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._status = ReconcileStatus()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._status.state == "running"

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._status.to_dict()

    def can_start(self) -> bool:
        """True if not already running AND last completion is far enough
        in the past that we haven't just finished a redundant pass."""
        with self._lock:
            if self._status.state == "running":
                return False
            if self._status.finished_at is None:
                return True
            since = datetime.now(UTC) - self._status.finished_at
            return since.total_seconds() >= RERUN_DEBOUNCE_SECONDS

    def start(self) -> bool:
        """Spawn the reconciliation thread. Returns True if started, False if
        the runner is already running or still inside the debounce window.
        """
        with self._lock:
            if not self._can_start_locked():
                return False
            self._status = ReconcileStatus(
                state="running",
                started_at=datetime.now(UTC),
            )
        self._thread = threading.Thread(target=self._run, daemon=True, name="harmonist-reconcile")
        self._thread.start()
        return True

    def _can_start_locked(self) -> bool:
        if self._status.state == "running":
            return False
        if self._status.finished_at is None:
            return True
        since = datetime.now(UTC) - self._status.finished_at
        return since.total_seconds() >= RERUN_DEBOUNCE_SECONDS

    def _run(self) -> None:
        error: str | None = None
        try:
            self._runner_fn(self._update_status)
        except Exception as e:
            log.exception("reconcile run failed")
            error = str(e)
        finally:
            with self._lock:
                self._status.state = "idle"
                self._status.finished_at = datetime.now(UTC)
                self._status.current_item = ""
                self._status.last_error = error

    def _update_status(
        self,
        *,
        current_item: str = "",
        completed: int | None = None,
        total: int | None = None,
        inbox: int | None = None,
        library: int | None = None,
        new: int | None = None,
        needs_sync: int | None = None,
    ) -> None:
        """Callback handed to the runner_fn so it can report progress."""
        with self._lock:
            if current_item is not None:
                self._status.current_item = current_item
            if completed is not None:
                self._status.completed = completed
            if total is not None:
                self._status.total = total
            if inbox is not None:
                self._status.inbox = inbox
            if library is not None:
                self._status.library = library
            if new is not None:
                self._status.new = new
            if needs_sync is not None:
                self._status.needs_sync = needs_sync


def reconcile_pending_orphans(
    music_dir: Path,
    *,
    fetch_urls: Callable[[str], list[str]],
    status_updater: Callable[..., None] | None = None,
    rate_limit_seconds: float = MB_RATE_LIMIT_SECONDS,
    exempt_paths: set[Path] | None = None,
) -> dict[str, int]:
    """Walk music_dir; reconcile every NEW album with an MBID atom.

    Albums whose path is in `exempt_paths` are skipped. This is the
    mechanism that respects user intent after a Forget — without it, the
    auto-reconciliation would immediately re-create the sidecar the user
    just deleted. Exemption is in-memory only; server restart clears it.

    Yielded progress goes through status_updater. Returns final stats.
    """
    from harmonist import reconcile, scanner
    from harmonist.models import AlbumState

    terminal = {AlbumState.COMPLETE, AlbumState.INCOMPLETE}

    exempt = exempt_paths or set()
    albums = scanner.scan(music_dir)
    pending = [a for a in albums if a.state == AlbumState.NEW and a.path not in exempt]
    total = len(pending)

    # Base counts at start, EXCLUDING the orphans we're about to reconcile — an
    # un-reconciled orphan isn't in any count yet. The live counts below are
    # base + the running outcome tallies, so the UI shows the inbox/library
    # numbers move as reconcile files each orphan (no mid-pass rescan needed).
    pending_paths = {a.path for a in pending}
    base_library = sum(1 for a in albums if a.state in terminal)
    base_needs_sync = sum(1 for a in albums if a.state == AlbumState.NEEDS_SYNC)
    base_new = sum(1 for a in albums if a.state == AlbumState.NEW and a.path not in pending_paths)
    base_inbox = sum(1 for a in albums if a.state not in terminal and a.path not in pending_paths)

    completed = 0
    reconciled_bandcamp = 0
    reconciled_manual = 0
    skipped = 0
    errors = 0

    def _report() -> None:
        if status_updater:
            stuck = skipped + errors
            status_updater(
                completed=completed,
                library=base_library + reconciled_manual,
                needs_sync=base_needs_sync + reconciled_bandcamp,
                new=base_new + stuck,
                inbox=base_inbox + reconciled_bandcamp + stuck,
            )

    if status_updater:
        status_updater(total=total)
    _report()  # publish the base (all-zero deltas) before the first album
    log.info("Reconcile: %d orphan album(s) to check for a MusicBrainz Id", total)

    for idx, album in enumerate(pending, start=1):
        label = f"{album.artist} / {album.title}"
        if status_updater:
            status_updater(current_item=label)
        try:
            sc = reconcile.reconcile_album(album.path, fetch_urls=fetch_urls)
        except Exception as e:
            log.warning("Reconcile failed for %s: %s", label, e)
            errors += 1
            _report()
            continue
        if sc is None:
            skipped += 1
            log.debug("Reconcile skipped (no MusicBrainz Id in tags): %s", label)
        elif sc.store_url:
            reconciled_bandcamp += 1
            log.info("Reconciled %s → %s", label, sc.store_url)
        else:
            reconciled_manual += 1
            log.info("Reconciled %s → MBID %s", label, sc.mb_release_id)
        completed += 1
        _report()
        # Rate-limit MB queries — but ONLY when a lookup actually happened.
        # reconcile_album hits the network only when the tags carry an MBID;
        # a skip (no MBID, sc is None) makes no call, so don't pace it.
        if sc is not None and idx < total and rate_limit_seconds > 0:
            time.sleep(rate_limit_seconds)

    log.info(
        "Reconcile done: %d reconciled (%d bandcamp, %d manual), %d skipped (no MBID), %d failed",
        reconciled_bandcamp + reconciled_manual,
        reconciled_bandcamp,
        reconciled_manual,
        skipped,
        errors,
    )
    return {
        "total": total,
        "reconciled_bandcamp": reconciled_bandcamp,
        "reconciled_manual": reconciled_manual,
        "skipped": skipped,
        "errors": errors,
    }
