"""In-memory activity log — a bounded ring buffer of recent events.

Process-level only (NOT persisted to sidecars — see the sidecar-minimalism
rule). Lost on restart, which is fine for a "recent activity" feed. Fed from
two places: action outcomes (every `_flash_response` in the web layer) and a
logging handler that mirrors harmonist's own WARNING/ERROR log records so
background failures show up too.

Thread-safe: the sync / reconcile runners append from worker threads.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime

# Keep this modest — it's a glanceable feed, not an audit trail.
_MAX_EVENTS = 200

_Level = str  # "info" | "warning" | "error"


@dataclass(frozen=True)
class Event:
    ts: datetime
    level: _Level
    message: str


_LOCK = threading.Lock()
_EVENTS: deque[Event] = deque(maxlen=_MAX_EVENTS)


def record(message: str, level: _Level = "info") -> None:
    """Append an event. Safe to call from any thread."""
    message = (message or "").strip()
    if not message:
        return
    with _LOCK:
        _EVENTS.append(Event(ts=datetime.now(UTC), level=level, message=message))


def recent(limit: int = 100) -> list[Event]:
    """Most-recent-first list of up to `limit` events."""
    with _LOCK:
        items = list(_EVENTS)
    items.reverse()
    return items[:limit]


def clear() -> None:
    """Drop all events (used by tests / demo reset)."""
    with _LOCK:
        _EVENTS.clear()


class _ActivityLogHandler(logging.Handler):
    """Mirror harmonist log records (WARNING+) into the activity feed so
    background failures (sync errors, skipped albums, …) are visible."""

    def emit(self, rec: logging.LogRecord) -> None:
        try:
            msg = rec.getMessage()
        except Exception:
            return
        level = "error" if rec.levelno >= logging.ERROR else "warning"
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
