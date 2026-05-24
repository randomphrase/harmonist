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
        new_items = 0
        error: str | None = None
        try:
            result = self._runner_fn()
            new_items = int(getattr(result, "new_items_downloaded", False))
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
