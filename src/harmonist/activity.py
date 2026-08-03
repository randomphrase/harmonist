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
    # The album this event was about, if any — carried through so the feed can
    # link the entry to that album (#65). An id recorded here is a snapshot: it
    # can go stale when reconcile rewrites an album's identity, so readers must
    # resolve it rather than trust it (see `_find_album`).
    album_id: str | None = None
    # The album's human name, frozen when the event was written. Rendered as the
    # entry's album column; `album_id` only decides whether it's a link.
    album_label: str | None = None
    # The action that produced this entry — the key its audit detail hangs off (#84).
    action_id: str | None = None


log = logging.getLogger(__name__)
_LOG_LEVELS = {
    Level.INFO: logging.INFO,
    Level.WARNING: logging.WARNING,
    Level.ERROR: logging.ERROR,
}


def record(
    message: str,
    level: Level = Level.INFO,
    *,
    album_id: str | None = None,
    album_label: str | None = None,
) -> None:
    """Append an event (Activity feed) AND emit it to the log, so the docker
    log is a superset of the feed. `album_id` ties the event to an album for
    per-album history (#33); None when it isn't about one album. `album_label`
    is that album's display name, stored so the entry stays readable after the
    id stops resolving (#65). Safe to call from any thread."""
    message = (message or "").strip()
    if not message:
        return
    activity_store.append(
        message=message,
        level=level,
        source=Source.ACTIVITY,
        album_id=album_id,
        album_label=album_label,
    )
    # Mirror to the log. The `_activity` flag stops _ActivityLogHandler from
    # re-recording it (which would feed back into this function — a loop).
    log.log(_LOG_LEVELS.get(level, logging.INFO), "%s", message, extra={"_activity": True})


def info(message: str, *, album_id: str | None = None, album_label: str | None = None) -> None:
    """Record an info-level activity event (logging-style shorthand for record())."""
    record(message, Level.INFO, album_id=album_id, album_label=album_label)


def warning(message: str, *, album_id: str | None = None, album_label: str | None = None) -> None:
    """Record a warning-level activity event."""
    record(message, Level.WARNING, album_id=album_id, album_label=album_label)


def error(message: str, *, album_id: str | None = None, album_label: str | None = None) -> None:
    """Record an error-level activity event."""
    record(message, Level.ERROR, album_id=album_id, album_label=album_label)


def recent(limit: int = 100) -> list[Event]:
    """Most-recent-first list of up to `limit` activity events."""
    return [
        Event(
            ts=e.ts,
            level=e.level,
            message=e.message,
            album_id=e.album_id,
            album_label=e.album_label,
            action_id=e.action_id,
        )
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
