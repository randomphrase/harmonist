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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional


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
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    current_item: str = ""
    completed: int = 0
    total: int = 0
    last_error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "current_item": self.current_item,
            "completed": self.completed,
            "total": self.total,
            "last_error": self.last_error,
        }


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ReconcileRunner:
    """Owns a single background reconciliation job at a time."""

    def __init__(self, runner_fn: Callable[..., None]):
        """`runner_fn(status_updater)` is the callable that iterates orphans
        and reconciles them. The runner injects a status updater so the
        function can report progress (current_item, completed counters).
        """
        self._runner_fn = runner_fn
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._status = ReconcileStatus()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._status.state == "running"

    def status(self) -> dict:
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
            since = datetime.now(timezone.utc) - self._status.finished_at
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
                started_at=datetime.now(timezone.utc),
            )
        self._thread = threading.Thread(target=self._run, daemon=True, name="harmonist-reconcile")
        self._thread.start()
        return True

    def _can_start_locked(self) -> bool:
        if self._status.state == "running":
            return False
        if self._status.finished_at is None:
            return True
        since = datetime.now(timezone.utc) - self._status.finished_at
        return since.total_seconds() >= RERUN_DEBOUNCE_SECONDS

    def _run(self) -> None:
        error: Optional[str] = None
        try:
            self._runner_fn(self._update_status)
        except Exception as e:
            log.exception("reconcile run failed")
            error = str(e)
        finally:
            with self._lock:
                self._status.state = "idle"
                self._status.finished_at = datetime.now(timezone.utc)
                self._status.current_item = ""
                self._status.last_error = error

    def _update_status(self, *, current_item: str = "", completed: Optional[int] = None,
                       total: Optional[int] = None) -> None:
        """Callback handed to the runner_fn so it can report progress."""
        with self._lock:
            if current_item is not None:
                self._status.current_item = current_item
            if completed is not None:
                self._status.completed = completed
            if total is not None:
                self._status.total = total


def reconcile_pending_orphans(
    music_dir: Path,
    *,
    fetch_urls: Callable[[str], list],
    status_updater: Optional[Callable[..., None]] = None,
    rate_limit_seconds: float = MB_RATE_LIMIT_SECONDS,
    exempt_paths: Optional[set] = None,
) -> dict:
    """Walk music_dir; reconcile every Orphan with an MBID atom.

    Albums whose path is in `exempt_paths` are skipped. This is the
    mechanism that respects user intent after a Forget — without it, the
    auto-reconciliation would immediately re-create the sidecar the user
    just deleted. Exemption is in-memory only; server restart clears it.

    Yielded progress goes through status_updater. Returns final stats.
    """
    from harmonist import reconcile, scanner
    from harmonist.models import AlbumState

    exempt = exempt_paths or set()
    albums = scanner.scan(music_dir)
    orphans = [
        a for a in albums
        if a.state == AlbumState.ORPHAN and a.path not in exempt
    ]
    total = len(orphans)
    if status_updater:
        status_updater(total=total, completed=0)

    completed = 0
    reconciled_bandcamp = 0
    reconciled_manual = 0
    skipped = 0
    errors = 0

    for idx, album in enumerate(orphans, start=1):
        if status_updater:
            status_updater(current_item=f"{album.artist} / {album.title}",
                           completed=completed)
        try:
            sc = reconcile.reconcile_album(album.path, fetch_urls=fetch_urls)
        except Exception as e:
            log.warning("reconcile_album failed for %s: %s", album.path, e)
            errors += 1
            continue
        if sc is None:
            skipped += 1
        elif sc.source == "bandcamp":
            reconciled_bandcamp += 1
        else:
            reconciled_manual += 1
        completed += 1
        if status_updater:
            status_updater(completed=completed)
        # Rate-limit MB queries. reconcile_album only makes a network call
        # when an MBID was found; skipped albums (no MBID) make no call,
        # but we conservatively pace anyway.
        if idx < total and rate_limit_seconds > 0:
            time.sleep(rate_limit_seconds)

    return {
        "total": total,
        "reconciled_bandcamp": reconciled_bandcamp,
        "reconciled_manual": reconciled_manual,
        "skipped": skipped,
        "errors": errors,
    }
