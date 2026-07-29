"""Durable, append-only event store (SQLite) behind the activity feed and audit log.

Both `activity.record()` and `audit.record()` append here, tagged by `source`
("activity" | "audit"), so what Harmonist did survives a restart — the point of
issue #33. The activity feed reads recent `source="activity"` rows directly (no
in-memory buffer): a bounded `ORDER BY id DESC LIMIT n` on an indexed table is
cheap, and dropping the ring buffer trims memory.

A *separate* store, not sidecar fields — consistent with sidecar-minimalism.

Thread-safe: the sync / reconcile / scan runners append from worker threads while
request handlers read. One shared connection (`check_same_thread=False`) guarded by
a lock; WAL mode so a reader never blocks a writer at the SQLite level.

Until `init()` points it at a file (done once at app start), the store falls back to
an in-memory database so `record()` still works in unit tests and non-web contexts —
ephemeral, exactly like the old ring buffer.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

log = logging.getLogger(__name__)


class Source(StrEnum):
    """Which log a stored event belongs to. The feed shows ACTIVITY; AUDIT is the
    durable forensic trail (both share this store, distinguished by this column)."""

    ACTIVITY = "activity"
    AUDIT = "audit"


class Level(StrEnum):
    """Severity of a stored event — drives the status-pill colour and the mirrored
    log level. A str at heart (StrEnum), so it stores/compares as its value."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


_LOCK = threading.Lock()
_conn: sqlite3.Connection | None = None

# Forward-only schema migrations, applied via SQLite's built-in `PRAGMA
# user_version`. Entry i takes the DB from user_version i to i+1. On open, every
# migration past the DB's current version is applied in order, so an `activity.db`
# created by an older Harmonist upgrades in place. RULES: never edit or reorder a
# shipped entry — a schema change is always a NEW appended migration. Keep each
# migration's statements idempotent-free (they run exactly once, tracked by
# user_version). This is the responsible, dependency-free equivalent of Rails
# migrations for a small SQLite store.
_MIGRATIONS: tuple[tuple[str, ...], ...] = (
    # 0 -> 1: initial events table (durable activity + audit feed, issue #33).
    (
        """CREATE TABLE events (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      TEXT NOT NULL,
            level   TEXT NOT NULL,
            source  TEXT NOT NULL,
            message TEXT NOT NULL
        )""",
        "CREATE INDEX idx_events_source_id ON events (source, id)",
    ),
)

SCHEMA_VERSION = len(_MIGRATIONS)  # the version this build expects/creates


@dataclass(frozen=True)
class StoredEvent:
    ts: datetime
    level: Level
    source: Source
    message: str


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring `conn` up to SCHEMA_VERSION by applying pending forward migrations.
    Each step (its DDL + the user_version bump) commits atomically. Refuses a DB
    newer than this build understands, so a downgrade never writes a schema it
    can't honour."""
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"activity.db schema is at v{version}, newer than this build supports "
            f"(v{SCHEMA_VERSION}) — refusing to open it"
        )
    for target in range(version, SCHEMA_VERSION):
        conn.execute("BEGIN")
        try:
            for stmt in _MIGRATIONS[target]:
                conn.execute(stmt)
            # PRAGMA can't take a bind param; target+1 is a controlled int.
            conn.execute(f"PRAGMA user_version = {target + 1}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _open(db: str) -> sqlite3.Connection:
    # autocommit (isolation_level=None) so single appends persist immediately and
    # _migrate can drive its own BEGIN/COMMIT explicitly.
    conn = sqlite3.connect(db, check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    _migrate(conn)
    return conn


def init(db_path: Path | str) -> None:
    """Point the store at a file (call once at app start): create the parent dir,
    open the DB, and run migrations. Replaces any prior connection. If the DB can't
    be opened/migrated (e.g. a newer schema after a downgrade), degrade to an
    in-memory store and log loudly — the app still starts, just without durable
    history — rather than crash or corrupt the file."""
    global _conn
    try:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = _open(str(path))
    except Exception:
        log.exception(
            "activity store: could not open %s — falling back to in-memory (history "
            "will not persist)",
            db_path,
        )
        conn = _open(":memory:")
    with _LOCK:
        old, _conn = _conn, conn
    if old is not None:
        try:
            old.close()
        except sqlite3.Error:
            pass


def _ensure() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        # No app has initialised a file-backed store — fall back to an ephemeral
        # in-memory DB so record() never fails (matches the old ring buffer).
        _conn = _open(":memory:")
    return _conn


def append(*, message: str, level: Level, source: Source) -> None:
    """Append one event. Best-effort: a failure here (e.g. a teardown race in
    tests) must never crash the caller or the logging path."""
    message = (message or "").strip()
    if not message:
        return
    ts = datetime.now(UTC).isoformat()
    try:
        conn = _ensure()
        with _LOCK:
            conn.execute(
                "INSERT INTO events (ts, level, source, message) VALUES (?, ?, ?, ?)",
                (ts, level.value, source.value, message),
            )
            conn.commit()
    except sqlite3.Error:
        log.debug("activity_store append failed", exc_info=True, extra={"_activity": True})


def recent(limit: int = 100, *, source: Source | None = None) -> list[StoredEvent]:
    """Most-recent-first events, optionally filtered by source."""
    q = "SELECT ts, level, source, message FROM events"
    args: list[object] = []
    if source is not None:
        q += " WHERE source = ?"
        args.append(source.value)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    try:
        conn = _ensure()
        with _LOCK:
            rows = conn.execute(q, args).fetchall()
    except sqlite3.Error:
        return []
    return [
        StoredEvent(
            ts=datetime.fromisoformat(ts), level=Level(level), source=Source(src), message=msg
        )
        for ts, level, src, msg in rows
    ]


def clear() -> None:
    """Drop all events (tests / demo reset)."""
    try:
        conn = _ensure()
        with _LOCK:
            conn.execute("DELETE FROM events")
            conn.commit()
    except sqlite3.Error:
        pass
