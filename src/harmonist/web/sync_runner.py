"""In-process background runner for the Bandcamp sync job.

Single-flight: only one sync runs at a time (the second POST /sync is a 409).
Status is exposed via a simple dict for HTMX polling at /sync/status.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from harmonist import activity

log = logging.getLogger(__name__)


@dataclass
class SyncStatus:
    state: str = "idle"  # "idle" | "running"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_error: str | None = None
    new_items: int = 0
    current_item: str = ""  # what's downloading right now, while running

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "last_error": self.last_error,
            "new_items": self.new_items,
            "current_item": self.current_item,
        }


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class AlreadyRunningError(Exception):
    pass


class SyncRunner:
    """Owns a single background sync job at a time."""

    def __init__(self, runner_fn: Callable[[], Any]):
        """`runner_fn` is the callable that actually performs the sync.
        Typically a closure that constructs HarmonistSyncer with the right config.
        """
        self._runner_fn = runner_fn
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._status = SyncStatus()
        # One-shot override for the NEXT sync, set by the Sync popover: None =
        # auto-detect link-only mode (the default), True/False = force it. The
        # runner consumes and clears it. GIL-atomic; start() prevents overlap.
        self.link_only_override: bool | None = None
        # The FastAPI app, set in create_app — lets the runner read app.state.cfg
        # FRESH each run (config is created after this runner, so a captured cfg
        # would go stale on a Settings change).
        self.app: Any = None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._status.state == "running"

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._status.to_dict()

    def set_current_item(self, label: str) -> None:
        """Callback for the inner sync impl to report what it's working on now.

        Designed to be passed into HarmonistSyncer / demo.run_demo_sync so the
        UI can show 'Syncing: Artist / Album' while a download is in flight.
        """
        with self._lock:
            self._status.current_item = label

    def start(self) -> SyncStatus:
        """Spawn a background thread that runs the sync. Raises if already running."""
        with self._lock:
            if self._status.state == "running":
                raise AlreadyRunningError("sync is already running")
            self._status = SyncStatus(
                state="running",
                started_at=datetime.now(UTC),
            )
        self._thread = threading.Thread(target=self._run, daemon=True, name="harmonist-sync")
        self._thread.start()
        return self._status

    def _run(self) -> None:
        activity.record("Bandcamp sync started", "info")
        new_items = 0
        remaining = 0
        error: str | None = None
        try:
            result = self._runner_fn()
            # Real syncer exposes an int `new_items` (count of downloads); the
            # demo result exposes a `new_items_downloaded` bool. Prefer the count.
            new_items = int(
                getattr(result, "new_items", getattr(result, "new_items_downloaded", 0))
            )
            # Albums deferred because the per-sync download limit was reached.
            remaining = int(getattr(result, "skipped_for_limit", 0))
        except Exception as e:
            log.exception("sync failed")
            error = str(e)
        finally:
            with self._lock:
                self._status.state = "idle"
                self._status.finished_at = datetime.now(UTC)
                self._status.last_error = error
                self._status.new_items = new_items
                self._status.current_item = ""
        if error:
            activity.record(f"Bandcamp sync failed — {error}", "error")
        else:
            plural = "" if new_items == 1 else "s"
            msg = f"Bandcamp sync finished — {new_items} new item{plural}"
            if remaining:
                msg += f"; {remaining} more reached the per-sync limit — run Sync again"
            activity.record(msg, "info")
