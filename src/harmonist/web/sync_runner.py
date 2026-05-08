"""In-process background runner for the Bandcamp sync job.

Single-flight: only one sync runs at a time (the second POST /sync is a 409).
Status is exposed via a simple dict for HTMX polling at /sync/status.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional


log = logging.getLogger(__name__)


@dataclass
class SyncStatus:
    state: str = "idle"  # "idle" | "running"
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    last_error: Optional[str] = None
    new_items: int = 0

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "last_error": self.last_error,
            "new_items": self.new_items,
        }


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        self._thread: Optional[threading.Thread] = None
        self._status = SyncStatus()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._status.state == "running"

    def status(self) -> dict:
        with self._lock:
            return self._status.to_dict()

    def start(self) -> SyncStatus:
        """Spawn a background thread that runs the sync. Raises if already running."""
        with self._lock:
            if self._status.state == "running":
                raise AlreadyRunningError("sync is already running")
            self._status = SyncStatus(
                state="running",
                started_at=datetime.now(timezone.utc),
            )
        self._thread = threading.Thread(target=self._run, daemon=True, name="harmonist-sync")
        self._thread.start()
        return self._status

    def _run(self) -> None:
        new_items = 0
        error: Optional[str] = None
        try:
            result = self._runner_fn()
            new_items = int(getattr(result, "new_items_downloaded", False))
        except Exception as e:
            log.exception("sync failed")
            error = str(e)
        finally:
            with self._lock:
                self._status.state = "idle"
                self._status.finished_at = datetime.now(timezone.utc)
                self._status.last_error = error
                self._status.new_items = new_items
