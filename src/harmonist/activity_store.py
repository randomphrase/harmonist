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

import contextvars
import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# Every failure logged from THIS module carries `_activity`, which tells the
# feed's log mirror (activity._ActivityLogHandler) not to turn it into a feed
# entry. Two reasons: writing "the store is broken" INTO the broken store is
# self-defeating, and the mirror's own append would fail and log again. The
# guard makes that terminate, but the pointless round trip is worth skipping.
_QUIET_MIRROR = {"_activity": True}


class StoreUnavailableError(RuntimeError):
    """A read against the store failed — the answer is unknown, not empty.

    Every read here used to swallow `sqlite3.Error` and return `[]` / `{}` /
    `None`, which the caller cannot tell from a genuine empty result. The album
    page then renders "Nothing recorded for this album yet" over a locked or
    corrupt database, stating as fact that Harmonist has never touched the album
    — at the moment the user is trying to find out what it did (#104).

    So reads raise this instead, and the routes turn it into a visible "history
    unavailable" rather than a confident lie. WRITES do not raise: recording is
    incidental to the operation being recorded and must never abort it, so they
    log at ERROR and continue. See the error-handling skill §1 and §5.
    """


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
    # 1 -> 2: per-album reference (an album's `Album.id`) so events can be filtered
    # by album — the foundation for per-album history (#14) and the gardener (#32).
    # Nullable: events not about one album (sync started, settings updated) have
    # none. Set on BOTH sources, so one query returns an album's activity + audit.
    (
        "ALTER TABLE events ADD COLUMN album_id TEXT",
        "CREATE INDEX idx_events_album_id ON events (album_id, id)",
    ),
    # 2 -> 3: human label for the album an event is about ("Artist — Title"),
    # frozen at write time (#65). An id alone is not enough to render history:
    # album ids MOVE (tagging drops the sidecar's temp_uid for the MBID; unlink
    # reverses it), so an older row's album_id often no longer resolves — and an
    # album can leave the library entirely. The label keeps the entry readable
    # regardless; the id is only used to decide whether to LINK it. Nullable:
    # events not about one album, and rows written before this migration, have
    # none. No index — it's display-only, never a query key.
    ("ALTER TABLE events ADD COLUMN album_label TEXT",),
    # 3 -> 4: album identity aliases (#33). Album ids MOVE: tagging drops the
    # sidecar's temp_uid in favour of the MBID, a re-match rewrites the MBID, an
    # unlink reverses it. Crucially the superseded id is ERASED from disk, so the
    # link between an album's past and present identity is observable ONLY at the
    # instant it changes — after that, nothing can reconstruct it. Recording the
    # pair keeps an album's history joinable across the flip (#14) and lets a
    # deep link written under the old id still resolve.
    # old_id is the PK: an id is superseded exactly once, so it maps forward to at
    # most one successor. new_id is indexed for the reverse walk (#14's union).
    (
        """CREATE TABLE album_aliases (
            old_id TEXT PRIMARY KEY,
            new_id TEXT NOT NULL,
            ts     TEXT NOT NULL
        )""",
        "CREATE INDEX idx_album_aliases_new_id ON album_aliases (new_id)",
    ),
    # 4 -> 5: correlation id tying one user-visible ACTION to every row it
    # produced — the activity entry AND the audit records underneath it (#84).
    # album_id (migration 1->2) joins at album granularity, which answers "what
    # happened to this album?" but not "what did THIS action do?" — the finer
    # question Revert (#32) must answer, since reverting the wrong change is
    # worse than not offering it. Nullable: bookkeeping outside any action
    # scope, and every row written before this migration, have none.
    (
        "ALTER TABLE events ADD COLUMN action_id TEXT",
        "CREATE INDEX idx_events_action_id ON events (action_id, id)",
    ),
    # 5 -> 6: per-field before/after detail for one tagged file (#86). A side
    # table rather than a column on `events`, because `events` is scanned on
    # every feed poll and SQLite stores rows contiguously — a multi-KB JSON
    # payload would inflate the pages the feed walks past even though the feed
    # never selects it. Here it costs the feed nothing and joins only when an
    # album page asks.
    #
    # ONE ROW PER FILE, not per album. An album-level field (label, barcode)
    # changes identically across every track, so it looks wasteful — but the
    # BEFORE values need not be identical: an album tagged unevenly over the
    # years is exactly why `compare.consensus` exists. Recording the album's
    # "before" once would have to pick one, and a revert would then write that
    # guess over the tracks it didn't come from. Aggregation to "album" vs
    # "all 18 tracks" happens at render time from `owned.Scope`, where being
    # approximate is free.
    #
    # event_id is the PK: it details exactly one `tag.track` audit row, which
    # also gives the join its index. `changes` is JSON {field: [before, after]},
    # keyed by `owned.Owned` values.
    #
    # FOUR WAYS TO NAME THE SAME TRACK, because each fails differently and this
    # table is append-only — a row cannot gain a better identifier later, and
    # which file carried which MusicBrainz identity is observable only at the
    # instant it is written (the same argument the 3->4 album_aliases migration
    # makes, one level down):
    #
    #   file       — breaks on a rename (#13 renames files deliberately)
    #   track_ref  — mb_release_track_id: breaks when re-matched to another release
    #   rec_ref    — mb_track_id (recording): usually survives that re-match, but
    #                can repeat within one release, so it can't identify alone
    #   position   — breaks on a renumber
    #
    # No two fail together. Only `file` has a reader today (the per-track
    # expansion displays it); the rest are what will let Revert (#32) find the
    # file a record belongs to, and without them every row written before that
    # lands would be permanently unrevertable. All nullable — a release where
    # MusicBrainz carries no track ids legitimately has neither ref.
    #
    # Identity lives in columns, not in `changes`: the JSON keys are the owned-
    # field vocabulary, and mixing identity into them would corrupt it.
    (
        """CREATE TABLE tag_changes (
            event_id  INTEGER PRIMARY KEY REFERENCES events (id),
            file      TEXT NOT NULL,
            track_ref TEXT,
            rec_ref   TEXT,
            position  TEXT,
            changes   TEXT NOT NULL
        )""",
    ),
)

SCHEMA_VERSION = len(_MIGRATIONS)  # the version this build expects/creates


# The action currently in scope, if any. A ContextVar rather than a parameter
# because audit.record() is called from deep inside sidecar.py / bandcamp_hook.py,
# far from anything that knows which user action is running — threading an id
# through those signatures would touch every layer in between.
#
# Thread semantics are exactly what's wanted here, and are load-bearing:
#   * Starlette copies the context into its threadpool, so a scope opened in HTTP
#     middleware IS visible to a sync route handler.
#   * A plain threading.Thread does NOT inherit it, so the sync/reconcile runners
#     can't pick up a stale request's id. They open their own scope.
_current_action: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "harmonist_action_id", default=None
)


@contextmanager
def action() -> Iterator[str]:
    """Scope one user-visible action: every event appended inside it — the
    activity entry AND the audit records underneath — shares one `action_id`.

    Scope per OUTCOME, not per run: a sync downloading ten albums should open ten
    scopes, so each is separately revertible (#32), not one blob covering all ten.

    Nesting keeps the outermost id, so a helper that opens its own scope inside a
    request doesn't fragment that request's records.
    """
    existing = _current_action.get()
    if existing is not None:
        yield existing
        return
    action_id = uuid.uuid4().hex
    token = _current_action.set(action_id)
    try:
        yield action_id
    finally:
        _current_action.reset(token)


def current_action() -> str | None:
    """The action id in scope, or None outside one."""
    return _current_action.get()


@dataclass(frozen=True)
class StoredEvent:
    ts: datetime
    level: Level
    source: Source
    message: str
    album_id: str | None = None
    # Display label for `album_id`'s album, frozen at write time (#65) — ids move,
    # so this is what keeps an old entry readable after its link goes stale.
    album_label: str | None = None
    # The action that produced this row — shared with every other row from the
    # same user-visible outcome (#84). None outside an action scope.
    action_id: str | None = None


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


def init_memory() -> None:
    """Use a throwaway in-memory store, touching no file. For demo mode: demo
    sandboxes the music dir but shares the real config dir, so opening the
    on-disk DB there would append demo events to the user's genuine history —
    the same reason demo never writes to the configured music_dir."""
    global _conn
    conn = _open(":memory:")
    with _LOCK:
        old, _conn = _conn, conn
    _close_quietly(old)


def _close_quietly(conn: sqlite3.Connection | None) -> None:
    if conn is None:
        return
    try:
        conn.close()
    except sqlite3.Error:
        pass


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
    _close_quietly(old)


def _ensure() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        # No app has initialised a file-backed store — fall back to an ephemeral
        # in-memory DB so record() never fails (matches the old ring buffer).
        _conn = _open(":memory:")
    return _conn


def append(
    *,
    message: str,
    level: Level,
    source: Source,
    album_id: str | None = None,
    album_label: str | None = None,
) -> int | None:
    """Append one event, returning its row id — or None when nothing was written.

    `album_id` (an album's `Album.id`) ties the event to an
    album so per-album history spans both sources; None when it isn't about one
    album. `album_label` is that album's human name, stored alongside because the
    id can stop resolving later (see the 2->3 migration). Best-effort: a failure here (e.g. a teardown race in tests) must never
    crash the caller or the logging path.

    The row id exists so a caller can attach structured detail to the row it
    just wrote (`record_tag_changes`, #86). None is therefore not just "nothing
    happened" — it means there is no row to hang detail off, and the caller must
    not treat the write as having succeeded.
    """
    message = (message or "").strip()
    if not message:
        return None
    ts = datetime.now(UTC).isoformat()
    try:
        conn = _ensure()
        with _LOCK:
            cur = conn.execute(
                "INSERT INTO events "
                "(ts, level, source, message, album_id, album_label, action_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    ts,
                    level.value,
                    source.value,
                    message,
                    album_id,
                    album_label,
                    _current_action.get(),
                ),
            )
            conn.commit()
            return int(cur.lastrowid) if cur.lastrowid else None
    except sqlite3.Error:
        # ERROR, not debug: this is the single sink for BOTH activity and audit,
        # so a dropped row here can be the only record that Harmonist rewrote the
        # user's tags. The audit log's whole value is "if it isn't here, it didn't
        # happen" — dropping rows quietly makes every remaining row untrustworthy
        # (skill §5). Still swallowed: recording must not abort what it records.
        #
        # `extra={"_activity": True}` is load-bearing — it stops the feed's log
        # mirror re-entering record() and looping (activity._ActivityLogHandler).
        log.exception("activity_store append failed", extra=_QUIET_MIRROR)
    return None


@dataclass(frozen=True)
class TagChanges:
    """One file's per-field before/after detail, and four ways to find that file
    again (see the 5->6 migration for why four)."""

    file: str
    changes: dict[str, Any]
    track_ref: str | None = None
    rec_ref: str | None = None
    position: str | None = None


def record_tag_changes(
    event_id: int,
    *,
    file: str,
    changes: Mapping[str, object],
    track_ref: str | None = None,
    rec_ref: str | None = None,
    position: str | None = None,
) -> None:
    """Attach one file's per-field before/after detail to the `tag.track` row
    `event_id` (#86).

    `changes` is `{owned_field: [before, after]}` and must be non-empty — a file
    whose tags didn't change writes no audit row at all, so there is nothing for
    a diff to hang off. Values are whatever the field holds: a string, None for
    absent, or a list for the multi-valued fields (`artists`, `isrcs`).

    The three refs identify the file as it stands *after* the write, which is
    what a later revert has to match against.

    Best-effort for the same reason `append` is: recording must never abort the
    tagging it records. A lost detail row costs the *disclosure*, not the audit
    line above it, which is already committed.
    """
    if not changes:
        return
    try:
        conn = _ensure()
        with _LOCK:
            conn.execute(
                "INSERT INTO tag_changes "
                "(event_id, file, track_ref, rec_ref, position, changes) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    file,
                    track_ref,
                    rec_ref,
                    position,
                    json.dumps(changes, ensure_ascii=False, sort_keys=True),
                ),
            )
            conn.commit()
    except sqlite3.Error:
        log.exception("activity_store record_tag_changes failed", extra=_QUIET_MIRROR)


def tag_changes_for(event_ids: Sequence[int]) -> dict[int, TagChanges]:
    """The stored detail for `event_ids`, keyed by event id.

    Takes the ids in one call rather than one query per row: an album's history
    can carry a `tag.track` row per file per tagging, and the page renders them
    together. Rows with no detail — every event that isn't a tagged file — are
    simply absent from the result.

    A payload that won't parse is skipped with a warning rather than raised: the
    records are permanent and unversioned, so one malformed row from a future or
    corrupted write must not take a whole album page down with it.
    """
    if not event_ids:
        return {}
    placeholders = ",".join("?" * len(event_ids))
    out: dict[int, TagChanges] = {}
    try:
        conn = _ensure()
        with _LOCK:
            rows = conn.execute(
                "SELECT event_id, file, track_ref, rec_ref, position, changes "
                f"FROM tag_changes WHERE event_id IN ({placeholders})",
                list(event_ids),
            ).fetchall()
    except sqlite3.Error:
        log.exception("activity_store tag_changes_for failed", extra=_QUIET_MIRROR)
        return {}
    for event_id, file, track_ref, rec_ref, position, payload in rows:
        try:
            parsed = json.loads(payload)
        except (TypeError, ValueError):
            log.warning("tag_changes row %s has unreadable JSON — skipping", event_id)
            continue
        if isinstance(parsed, dict):
            out[int(event_id)] = TagChanges(
                file=str(file),
                changes=parsed,
                track_ref=track_ref,
                rec_ref=rec_ref,
                position=position,
            )
    return out


def recent(
    limit: int = 100,
    *,
    source: Source | None = None,
    album_id: str | None = None,
    offset: int = 0,
) -> list[StoredEvent]:
    """Most-recent-first events, optionally filtered by source and/or album.
    Filtering by `album_id` alone returns that album's whole history — activity
    AND audit.

    `offset` pages backwards through history. Callers wanting to know whether
    more exist should ask for `limit + 1` and check the length rather than
    issuing a COUNT: this table grows without bound and is re-read every couple
    of seconds, so a full count per page would be the expensive part."""
    q = "SELECT ts, level, source, message, album_id, album_label, action_id FROM events"
    where: list[str] = []
    args: list[object] = []
    if source is not None:
        where.append("source = ?")
        args.append(source.value)
    if album_id is not None:
        where.append("album_id = ?")
        args.append(album_id)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY id DESC LIMIT ? OFFSET ?"
    args.append(limit)
    args.append(max(0, offset))
    try:
        conn = _ensure()
        with _LOCK:
            rows = conn.execute(q, args).fetchall()
    except sqlite3.Error as exc:
        log.exception(
            "activity_store recent() failed — the feed cannot be read", extra=_QUIET_MIRROR
        )
        raise StoreUnavailableError("could not read the activity store") from exc
    return [
        StoredEvent(
            ts=datetime.fromisoformat(ts),
            level=Level(level),
            source=Source(src),
            message=msg,
            album_id=aid,
            album_label=label,
            action_id=act,
        )
        for ts, level, src, msg, aid, label, act in rows
    ]


def version() -> str:
    """A token that changes iff the stored events have. Cheap enough to compute
    on every feed poll (#118).

    `MAX(id)` alone would nearly do, since the table is append-only and ids only
    go up. `COUNT(*)` rides along so `clear()` (demo reset, tests) also moves the
    token: it deletes every row, after which the next MAX could otherwise repeat
    a value the client has already seen.

    Raises rather than returning a fixed token on failure: a constant would mean
    "nothing has changed" forever, freezing the feed on whatever it last showed —
    the same class of lie as returning an empty list (error-handling skill §1).
    """
    try:
        conn = _ensure()
        with _LOCK:
            row = conn.execute("SELECT MAX(id), COUNT(*) FROM events").fetchone()
    except sqlite3.Error as exc:
        log.exception("activity_store version() failed", extra=_QUIET_MIRROR)
        raise StoreUnavailableError("could not read the store version") from exc
    return f"{row[0] or 0}.{row[1] or 0}"


#: The audit type written when the scanner first meets an album (#107). Also
#: serves as the "have we met?" marker — see `already_discovered`.
DISCOVERY_EVENT = "album.discovered"


def already_discovered(album_ids: list[str]) -> set[str]:
    """Which of `album_ids` already have a discovery record.

    The record IS the ledger, so there is no separate table to keep in step: an
    album has been met iff a row says so. That only works because a sidecar-less
    album's id is now derived from its path and so is stable across restarts
    (#114) — with the old per-process UUIDs this question was unanswerable.

    One indexed query per scan regardless of library size; the scan runs
    constantly, so a per-album lookup would be the expensive part.
    """
    if not album_ids:
        return set()
    placeholders = ",".join("?" * len(album_ids))
    q = (
        f"SELECT DISTINCT album_id FROM events WHERE album_id IN ({placeholders}) "
        "AND source = ? AND message LIKE ?"
    )
    try:
        conn = _ensure()
        with _LOCK:
            rows = conn.execute(
                q, [*album_ids, Source.AUDIT.value, f"{DISCOVERY_EVENT} %"]
            ).fetchall()
    except sqlite3.Error as exc:
        log.exception("activity_store already_discovered() failed", extra=_QUIET_MIRROR)
        raise StoreUnavailableError("could not read the discovery records") from exc
    return {r[0] for r in rows}


def audit_by_action(action_ids: list[str]) -> dict[str, list[StoredEvent]]:
    """Audit rows for each of `action_ids`, grouped — the "what changed" detail
    under an activity entry (#84).

    Takes a LIST and returns the whole grouping in ONE query: the feed renders up
    to 100 entries and re-polls every couple of seconds, so a per-entry lookup
    would be an N+1 against a table that grows without bound.
    """
    if not action_ids:
        return {}
    placeholders = ",".join("?" * len(action_ids))
    q = (
        "SELECT ts, level, source, message, album_id, album_label, action_id "
        f"FROM events WHERE source = ? AND action_id IN ({placeholders}) ORDER BY id"
    )
    try:
        conn = _ensure()
        with _LOCK:
            rows = conn.execute(q, [Source.AUDIT.value, *action_ids]).fetchall()
    except sqlite3.Error as exc:
        log.exception("activity_store audit_by_action() failed", extra=_QUIET_MIRROR)
        raise StoreUnavailableError("could not read the audit detail") from exc
    out: dict[str, list[StoredEvent]] = {}
    for ts, level, src, msg, aid, label, act in rows:
        out.setdefault(act, []).append(
            StoredEvent(
                ts=datetime.fromisoformat(ts),
                level=Level(level),
                source=Source(src),
                message=msg,
                album_id=aid,
                album_label=label,
                action_id=act,
            )
        )
    return out


def audit_without_action(since: datetime, limit: int = 200) -> list[StoredEvent]:
    """Audit rows written OUTSIDE any action scope, newest first, no older than
    `since`.

    These have no activity entry to sit beneath, so the feed shows them as rows
    of their own — without this they'd be invisible in the UI entirely.

    Most writes ARE scoped: the HTTP middleware wraps every mutating request, and
    reconcile opens a scope per album. The ones that aren't come from background
    passes with no enclosing request — the post-sync surrender / unmatched-purchase
    sweep in particular, whose rows are the only record that a surrender happened
    (#123).

    `since` bounds them to the page being rendered; without it, one ancient
    orphan would surface at the top of page 1 forever.
    """
    q = (
        "SELECT ts, level, source, message, album_id, album_label, action_id "
        "FROM events WHERE source = ? AND action_id IS NULL AND ts >= ? "
        "ORDER BY id DESC LIMIT ?"
    )
    try:
        conn = _ensure()
        with _LOCK:
            rows = conn.execute(q, (Source.AUDIT.value, since.isoformat(), limit)).fetchall()
    except sqlite3.Error as exc:
        log.exception("activity_store audit_without_action() failed", extra=_QUIET_MIRROR)
        raise StoreUnavailableError("could not read the unscoped audit rows") from exc
    return [
        StoredEvent(
            ts=datetime.fromisoformat(ts),
            level=Level(level),
            source=Source(src),
            message=msg,
            album_id=aid,
            album_label=label,
            action_id=act,
        )
        for ts, level, src, msg, aid, label, act in rows
    ]


def record_alias(old_id: str, new_id: str) -> None:
    """Remember that `old_id` became `new_id` (#33).

    Called at the moment an album's identity changes, which is the only time the
    pair is knowable — the superseded id is erased from the sidecar, so this is
    not reconstructible after the fact. Best-effort: identity changes must not
    fail because the log is unavailable.

    Idempotent via REPLACE: re-writing the same sidecar re-asserts the same pair
    rather than erroring on the primary key.
    """
    if not old_id or not new_id or old_id == new_id:
        return
    ts = datetime.now(UTC).isoformat()
    try:
        conn = _ensure()
        with _LOCK:
            conn.execute(
                "INSERT OR REPLACE INTO album_aliases (old_id, new_id, ts) VALUES (?, ?, ?)",
                (old_id, new_id, ts),
            )
            conn.commit()
    except sqlite3.Error:
        # ERROR and unrecoverable: this insert is the ONLY moment the old->new
        # pair is knowable (the superseded id is erased from the sidecar straight
        # afterwards), so losing it orphans the album's entire pre-tag history and
        # 404s every deep link written under the old id, permanently. Still
        # swallowed — an identity change must not fail because the log is down.
        log.exception(
            "activity_store record_alias failed — %s -> %s; this album's history "
            "before the change is now unreachable and old links to it will 404",
            old_id,
            new_id,
            extra=_QUIET_MIRROR,
        )


def resolve_alias(album_id: str, *, max_hops: int = 20) -> str | None:
    """Follow the alias chain forward to the album's CURRENT id, or None if this
    id was never superseded.

    Walks transitively (temp_uid -> mbid -> corrected mbid). `max_hops` and the
    seen-set bound the walk: a cycle would otherwise spin forever, and while the
    schema shouldn't permit one, a log is not worth hanging a request over.

    Raises `StoreUnavailableError` rather than returning None on a read failure:
    None here means "never superseded", which sends the only caller on to a 404
    — so one transient DB error would tell the user an album that is sitting on
    disk, intact, does not exist (#104, skill §1).
    """
    seen = {album_id}
    current = album_id
    try:
        conn = _ensure()
        with _LOCK:
            for _ in range(max_hops):
                row = conn.execute(
                    "SELECT new_id FROM album_aliases WHERE old_id = ?", (current,)
                ).fetchone()
                if row is None or row[0] in seen:
                    break
                current = row[0]
                seen.add(current)
    except sqlite3.Error as exc:
        log.exception("activity_store resolve_alias(%s) failed", album_id, extra=_QUIET_MIRROR)
        raise StoreUnavailableError("could not follow the album alias chain") from exc
    return None if current == album_id else current


def album_history(album_id: str, limit: int = 200, *, offset: int = 0) -> list[StoredEvent]:
    """Everything recorded about one album — activity AND audit — newest first.

    Unions over the album's ALIAS CHAIN, which is the whole point: an album's
    records are spread across every id it has ever had. Tagging replaces a
    sidecar's temp_uid with the MBID, so querying the current id alone silently
    drops the album's entire pre-tag history — the records most likely to explain
    how it got into its current state.

    This is the consumer the alias capture (#73) was built for.
    """
    ids = [album_id, *_alias_ancestors(album_id)]
    placeholders = ",".join("?" * len(ids))
    q = (
        "SELECT ts, level, source, message, album_id, album_label, action_id "
        f"FROM events WHERE album_id IN ({placeholders}) "
        "ORDER BY id DESC LIMIT ? OFFSET ?"
    )
    # Deliberately NOT swallowed: an empty list here renders as "Nothing recorded
    # for this album yet", so hiding a read failure would have the page state, as
    # fact, that Harmonist has never touched the album — at the moment the user is
    # trying to find out what it did. Raised as StoreUnavailableError so the route
    # can say "history unavailable" instead. See the error-handling skill §1.
    try:
        conn = _ensure()
        with _LOCK:
            rows = conn.execute(q, [*ids, limit, max(0, offset)]).fetchall()
    except sqlite3.Error as exc:
        log.exception("activity_store album_history(%s) failed", album_id, extra=_QUIET_MIRROR)
        raise StoreUnavailableError("could not read this album's history") from exc
    return [
        StoredEvent(
            ts=datetime.fromisoformat(ts),
            level=Level(level),
            source=Source(src),
            message=msg,
            album_id=aid,
            album_label=label,
            action_id=act,
        )
        for ts, level, src, msg, aid, label, act in rows
    ]


def _alias_ancestors(album_id: str, *, max_hops: int = 20) -> list[str]:
    """Every id this album used to have, walking the alias chain BACKWARDS.

    `resolve_alias` walks forward (old -> current) to rescue a stale link; this
    walks the other way (current -> everything it superseded) to gather history.
    Breadth-first, because an id can have several predecessors — an album
    re-matched twice leaves two supplanted ids pointing at the same current one.

    Bounded by `max_hops` and a seen-set: a cycle would otherwise spin, and a log
    query is not worth hanging a request over.
    """
    found: list[str] = []
    seen = {album_id}
    frontier = [album_id]
    # Not swallowed: a partial chain silently narrows the history to a subset of
    # the album's ids, which looks exactly like a complete answer — and the old
    # code discarded the ids it HAD already walked on the way out. Propagate to
    # album_history's caller instead. See the error-handling skill §1.
    try:
        conn = _ensure()
        with _LOCK:
            for _ in range(max_hops):
                if not frontier:
                    break
                placeholders = ",".join("?" * len(frontier))
                rows = conn.execute(
                    f"SELECT old_id FROM album_aliases WHERE new_id IN ({placeholders})",
                    frontier,
                ).fetchall()
                frontier = [r[0] for r in rows if r[0] not in seen]
                seen.update(frontier)
                found.extend(frontier)
    except sqlite3.Error as exc:
        log.exception("activity_store _alias_ancestors(%s) failed", album_id, extra=_QUIET_MIRROR)
        raise StoreUnavailableError("could not read this album's identity history") from exc
    return found


def clear() -> None:
    """Drop all stored state — events AND aliases (tests / demo reset).

    Aliases go too: they describe albums in the library this store was recording,
    so after a demo re-seed (or between tests) they'd point at identities that no
    longer exist, and could mis-resolve a deep link to the wrong album.
    """
    try:
        conn = _ensure()
        with _LOCK:
            conn.execute("DELETE FROM events")
            conn.execute("DELETE FROM album_aliases")
            conn.commit()
    except sqlite3.Error:
        # Swallowed rather than raised: the callers are teardown paths (test
        # fixtures, the demo reset) where failing to wipe is not the user's work
        # and a raise would mask the real failure under a cleanup error. Loud,
        # though — a reset that didn't reset leaves stale aliases that can
        # mis-resolve a deep link to the wrong album.
        log.exception(
            "activity_store clear() failed — stored events may be stale", extra=_QUIET_MIRROR
        )
