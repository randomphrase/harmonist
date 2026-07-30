"""Tests for the durable activity/audit store (issue #33)."""

from __future__ import annotations

import sqlite3

import pytest

from harmonist import activity, activity_store, audit
from harmonist.activity_store import Level, Source


@pytest.fixture(autouse=True)
def _fresh_store(tmp_path):
    """Point the store at a fresh file per test and reset it afterwards."""
    activity_store.init(tmp_path / "activity.db")
    activity_store.clear()
    yield
    activity_store.clear()


def test_append_and_recent_roundtrip():
    activity_store.append(message="hello", level=Level.INFO, source=Source.ACTIVITY)
    events = activity_store.recent()
    assert [e.message for e in events] == ["hello"]
    assert events[0].level == "info"
    assert events[0].source == "activity"


def test_recent_is_most_recent_first_and_respects_limit():
    for i in range(5):
        activity_store.append(message=f"e{i}", level=Level.INFO, source=Source.ACTIVITY)
    msgs = [e.message for e in activity_store.recent(limit=3)]
    assert msgs == ["e4", "e3", "e2"]  # newest first, capped at 3


def test_source_filter_separates_activity_and_audit():
    activity_store.append(message="user action", level=Level.INFO, source=Source.ACTIVITY)
    activity_store.append(message="download url=x", level=Level.INFO, source=Source.AUDIT)
    assert [e.message for e in activity_store.recent(source=Source.ACTIVITY)] == ["user action"]
    assert [e.message for e in activity_store.recent(source=Source.AUDIT)] == ["download url=x"]
    assert {e.message for e in activity_store.recent()} == {"user action", "download url=x"}


def test_blank_messages_are_dropped():
    activity_store.append(message="   ", level=Level.INFO, source=Source.ACTIVITY)
    activity_store.append(message="", level=Level.INFO, source=Source.ACTIVITY)
    assert activity_store.recent() == []


def test_survives_reopen(tmp_path):
    """The whole point of #33: events persist across a restart (a fresh
    connection to the same file still sees them)."""
    db = tmp_path / "persist.db"
    activity_store.init(db)
    activity_store.clear()
    activity_store.append(message="before restart", level=Level.WARNING, source=Source.AUDIT)

    activity_store.init(db)  # simulate a restart: new connection, same file
    events = activity_store.recent()
    assert [e.message for e in events] == ["before restart"]
    assert events[0].level == "warning"


def test_activity_record_stores_as_activity_source():
    activity.record("did a thing")
    assert [e.message for e in activity_store.recent(source=Source.ACTIVITY)] == ["did a thing"]
    # The user feed reads it back.
    assert [e.message for e in activity.recent()] == ["did a thing"]


def test_audit_record_stores_as_audit_and_is_absent_from_the_feed():
    audit.record("tagged", album="/music/The Album", mbid="rel-1")
    stored = activity_store.recent(source=Source.AUDIT)
    assert len(stored) == 1
    # key=value; a path with whitespace is quoted so the line stays parseable.
    assert stored[0].message == 'tagged album="/music/The Album" mbid=rel-1'
    # The user-facing activity feed does NOT surface audit rows.
    assert activity.recent() == []


def test_level_roundtrips_as_enum():
    activity_store.append(message="boom", level=Level.ERROR, source=Source.ACTIVITY)
    e = activity_store.recent()[0]
    assert e.level is Level.ERROR
    # StrEnum stays a str, so the template's `e.level == 'error'` still matches
    # (checked via a str-typed alias — mypy rejects the literal comparison as
    # non-overlapping, but the runtime behaviour is what the template relies on).
    as_str: str = e.level
    assert as_str == "error"


def test_activity_level_wrappers_record_the_right_level():
    activity.info("fyi")
    activity.warning("careful")
    activity.error("broke")
    got = {(e.message, e.level) for e in activity_store.recent(source=Source.ACTIVITY)}
    assert ("fyi", Level.INFO) in got
    assert ("careful", Level.WARNING) in got
    assert ("broke", Level.ERROR) in got


def test_album_id_filter_spans_both_sources():
    """The point of album_id (#33): one query returns an album's whole history —
    the user-facing activity AND the forensic audit rows — and excludes other
    albums plus events not tied to any album."""
    activity.info("Re-tagged Album A", album_id="rel-a")
    audit.record("sidecar.update", album_id="rel-a", mbid="x->y")
    activity.info("Re-tagged Album B", album_id="rel-b")
    activity.info("Bandcamp sync started")  # not about one album

    mine = activity_store.recent(album_id="rel-a")
    assert {e.message for e in mine} == {"Re-tagged Album A", "sidecar.update mbid=x->y"}
    assert {e.source for e in mine} == {Source.ACTIVITY, Source.AUDIT}


def test_album_id_is_none_when_not_supplied():
    activity.info("Bandcamp sync started")
    assert activity_store.recent()[0].album_id is None


def test_album_id_not_embedded_in_the_audit_message():
    """album_id is a structured column, not part of the `key=value` line."""
    audit.record("download", album_id="rel-x", item_id=7)
    e = activity_store.recent(source=Source.AUDIT)[0]
    assert e.message == "download item_id=7"
    assert e.album_id == "rel-x"


def test_clear_empties_the_store():
    activity_store.append(message="x", level=Level.INFO, source=Source.ACTIVITY)
    activity_store.clear()
    assert activity_store.recent() == []


def _user_version(db) -> int:
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        (v,) = conn.execute("PRAGMA user_version").fetchone()
        return int(v)
    finally:
        conn.close()


def test_fresh_db_is_migrated_to_current_schema_version(tmp_path):
    db = tmp_path / "ver.db"
    activity_store.init(db)
    activity_store.append(message="x", level=Level.INFO, source=Source.ACTIVITY)
    assert activity_store.SCHEMA_VERSION >= 1
    assert _user_version(db) == activity_store.SCHEMA_VERSION


def test_migrates_a_v1_database_in_place_keeping_its_rows(tmp_path):
    """A v1 activity.db (no album_id column) written by an older Harmonist must
    upgrade in place on open — schema bumped, existing rows preserved and readable
    with album_id NULL. This is the migration machinery doing its job."""
    db = tmp_path / "v1.db"
    conn = sqlite3.connect(db, isolation_level=None)
    conn.executescript(
        """
        CREATE TABLE events (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      TEXT NOT NULL,
            level   TEXT NOT NULL,
            source  TEXT NOT NULL,
            message TEXT NOT NULL
        );
        CREATE INDEX idx_events_source_id ON events (source, id);
        """
    )
    conn.execute(
        "INSERT INTO events (ts, level, source, message) VALUES (?, ?, ?, ?)",
        ("2026-07-01T00:00:00+00:00", "info", "activity", "written by v1"),
    )
    conn.execute("PRAGMA user_version = 1")
    conn.close()

    activity_store.init(db)  # should migrate 1 -> current

    assert _user_version(db) == activity_store.SCHEMA_VERSION
    events = activity_store.recent()
    assert [e.message for e in events] == ["written by v1"]
    assert events[0].album_id is None  # the new column defaults to NULL
    # ...and the upgraded DB accepts album-tagged rows.
    activity.info("after migration", album_id="rel-new")
    assert [e.message for e in activity_store.recent(album_id="rel-new")] == ["after migration"]


def test_migrates_a_v2_database_in_place_keeping_its_rows(tmp_path):
    """The 2 -> 3 upgrade (album_label, #65): a v2 activity.db must gain the
    column in place, keep every existing row, and read the new column as NULL —
    those rows predate it, so there is nothing to back-fill them with.

    Built by applying a PREFIX of the real _MIGRATIONS rather than hand-written
    DDL, so this test can't drift from what shipped.
    """
    db = tmp_path / "v2.db"
    conn = sqlite3.connect(db, isolation_level=None)
    for step in activity_store._MIGRATIONS[:2]:
        for stmt in step:
            conn.execute(stmt)
    conn.execute(
        "INSERT INTO events (ts, level, source, message, album_id) VALUES (?, ?, ?, ?, ?)",
        ("2026-07-01T00:00:00+00:00", "info", "activity", "written by v2", "rel-old"),
    )
    conn.execute("PRAGMA user_version = 2")
    conn.close()

    activity_store.init(db)  # should migrate 2 -> current

    assert _user_version(db) == activity_store.SCHEMA_VERSION
    events = activity_store.recent()
    assert [e.message for e in events] == ["written by v2"]
    assert events[0].album_id == "rel-old"  # pre-existing data intact
    assert events[0].album_label is None  # nothing to back-fill it with
    # ...and the upgraded DB round-trips the new column.
    activity.info("after 2->3", album_id="rel-x", album_label="Boards of Canada — Geogaddi")
    got = activity_store.recent(album_id="rel-x")
    assert [e.album_label for e in got] == ["Boards of Canada — Geogaddi"]


def test_reopening_a_current_db_applies_nothing(tmp_path):
    """Idempotent re-open: opening an already-current DB must not re-run
    migrations or disturb existing rows."""
    db = tmp_path / "current.db"
    activity_store.init(db)
    activity.info("first run", album_id="rel-1", album_label="A — B")
    before = _user_version(db)

    activity_store.init(db)  # second open, same file

    assert _user_version(db) == before == activity_store.SCHEMA_VERSION
    events = activity_store.recent()
    assert [e.message for e in events] == ["first run"]
    assert events[0].album_label == "A — B"


def test_downgrade_guard_falls_back_to_memory_without_crashing(tmp_path):
    """A DB stamped newer than this build understands (a Harmonist downgrade) must
    not be written to or crash startup — the store degrades to in-memory."""
    db = tmp_path / "future.db"
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute(f"PRAGMA user_version = {activity_store.SCHEMA_VERSION + 5}")
    conn.close()

    activity_store.init(db)  # must not raise
    activity_store.append(message="still works", level=Level.INFO, source=Source.ACTIVITY)
    assert [e.message for e in activity_store.recent()] == ["still works"]

    # The future-versioned file was left untouched — no events table written to it.
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "events" not in tables
