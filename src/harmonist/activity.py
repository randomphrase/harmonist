"""The user-facing activity feed — action outcomes plus mirrored WARNING/ERROR
log records so background failures are visible.

Durability lives in `activity_store` (a shared SQLite store, issue #33): `record()`
appends there tagged `source="activity"`, and `recent()` reads those rows back, so
the feed survives a restart. This module keeps the public API (`record`, `recent`,
`Event`, `clear`, `install_log_handler`) and adds the log mirroring on top.

Thread-safe: the store serialises appends; the sync / reconcile runners record from
worker threads.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from . import activity_store
from .activity_store import Level, Source


@dataclass(frozen=True)
class Event:
    ts: datetime
    level: Level
    message: str


log = logging.getLogger(__name__)
_LOG_LEVELS = {
    Level.INFO: logging.INFO,
    Level.WARNING: logging.WARNING,
    Level.ERROR: logging.ERROR,
}


def record(message: str, level: Level = Level.INFO) -> None:
    """Append an event (Activity feed) AND emit it to the log, so the docker
    log is a superset of the feed. Safe to call from any thread."""
    message = (message or "").strip()
    if not message:
        return
    activity_store.append(message=message, level=level, source=Source.ACTIVITY)
    # Mirror to the log. The `_activity` flag stops _ActivityLogHandler from
    # re-recording it (which would feed back into this function — a loop).
    log.log(_LOG_LEVELS.get(level, logging.INFO), "%s", message, extra={"_activity": True})


def info(message: str) -> None:
    """Record an info-level activity event (logging-style shorthand for record())."""
    record(message, Level.INFO)


def warning(message: str) -> None:
    """Record a warning-level activity event."""
    record(message, Level.WARNING)


def error(message: str) -> None:
    """Record an error-level activity event."""
    record(message, Level.ERROR)


def recent(limit: int = 100) -> list[Event]:
    """Most-recent-first list of up to `limit` activity events."""
    return [
        Event(ts=e.ts, level=e.level, message=e.message)
        for e in activity_store.recent(limit, source=Source.ACTIVITY)
    ]


def clear() -> None:
    """Drop all stored events (used by tests / demo reset)."""
    activity_store.clear()


class _ActivityLogHandler(logging.Handler):
    """Mirror harmonist log records (WARNING+) into the activity feed so
    background failures (sync errors, skipped albums, …) are visible."""

    def emit(self, rec: logging.LogRecord) -> None:
        if getattr(rec, "_activity", False):
            return  # already recorded via record(); don't loop it back
        try:
            msg = rec.getMessage()
        except Exception:
            return
        level = Level.ERROR if rec.levelno >= logging.ERROR else Level.WARNING
        record(msg, level)


_handler_installed = False


def install_log_handler() -> None:
    """Attach the log->activity mirror to the `harmonist` logger. Idempotent
    (create_app may run many times in tests)."""
    global _handler_installed
    if _handler_installed:
        return
    handler = _ActivityLogHandler(level=logging.WARNING)
    logging.getLogger("harmonist").addHandler(handler)
    _handler_installed = True
