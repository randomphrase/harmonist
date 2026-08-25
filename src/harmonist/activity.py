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
    # Which sink wrote it. Only meaningful when the caller asked to include audit
    # rows: the feed renders those differently (technical, monospace) because
    # they're raw forensics rather than a user-facing outcome.
    source: Source = Source.ACTIVITY


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


def recent(limit: int = 100, *, offset: int = 0, include_audit: bool = False) -> list[Event]:
    """Most-recent-first list of up to `limit` activity events.

    `include_audit=True` interleaves the raw audit records too. Worth having as
    an opt-in rather than only inside an entry's "what changed" disclosure:
    audit rows written OUTSIDE an action scope (the post-sync surrender pass, for
    instance) have no `action_id`, so no disclosure can ever show them — without
    this they are reachable from nowhere in the UI.
    """
    source = None if include_audit else Source.ACTIVITY
    return [
        Event(
            ts=e.ts,
            level=e.level,
            message=e.message,
            album_id=e.album_id,
            album_label=e.album_label,
            action_id=e.action_id,
            source=e.source,
        )
        for e in activity_store.recent(limit, source=source, offset=offset)
    ]


def clear() -> None:
    """Drop all stored events (used by tests / demo reset)."""
    activity_store.clear()


def _album_ref(rec: logging.LogRecord) -> tuple[str | None, str | None]:
    """The album a log record names via `extra=`, or `(None, None)`.

    A call site that knows which album it is talking about says so —
    `log.warning(..., extra={"album_id": ..., "album_label": ...})` — and the
    mirrored entry then joins that album's history instead of floating in the
    global feed attributed to nothing (#260). The mirror used to call
    `record(msg, level)` and nothing else, so *every* mirrored line landed with
    a NULL album and `album_history`, which selects by id, could never find it.

    Opt-in on purpose: most WARNING+ records genuinely aren't about one album,
    and they go on mirroring unattributed exactly as before.

    Type-checked rather than trusted, because `extra` is an arbitrary dict — a
    caller passing a `Path` or an `Album` here would otherwise put a non-string
    into a TEXT column and break the reader, far from the call site that did it.
    """
    album_id = getattr(rec, "album_id", None)
    album_label = getattr(rec, "album_label", None)
    return (
        album_id if isinstance(album_id, str) else None,
        album_label if isinstance(album_label, str) else None,
    )


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
        album_id, album_label = _album_ref(rec)
        record(msg, level, album_id=album_id, album_label=album_label)


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
