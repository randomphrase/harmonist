"""Tests for the durable activity/audit store (issue #33)."""

from __future__ import annotations

import sqlite3

import pytest

from harmonist import activity, activity_store, audit
from harmonist.activity_store import Source


@pytest.fixture(autouse=True)
def _fresh_store(tmp_path):
    """Point the store at a fresh file per test and reset it afterwards."""
    activity_store.init(tmp_path / "activity.db")
    activity_store.clear()
    yield
    activity_store.clear()


def test_append_and_recent_roundtrip():
    activity_store.append(message="hello", level="info", source=Source.ACTIVITY)
    events = activity_store.recent()
    assert [e.message for e in events] == ["hello"]
    assert events[0].level == "info"
    assert events[0].source == "activity"


def test_recent_is_most_recent_first_and_respects_limit():
    for i in range(5):
        activity_store.append(message=f"e{i}", level="info", source=Source.ACTIVITY)
    msgs = [e.message for e in activity_store.recent(limit=3)]
    assert msgs == ["e4", "e3", "e2"]  # newest first, capped at 3


def test_source_filter_separates_activity_and_audit():
    activity_store.append(message="user action", level="info", source=Source.ACTIVITY)
    activity_store.append(message="download url=x", level="info", source=Source.AUDIT)
    assert [e.message for e in activity_store.recent(source=Source.ACTIVITY)] == ["user action"]
    assert [e.message for e in activity_store.recent(source=Source.AUDIT)] == ["download url=x"]
    assert {e.message for e in activity_store.recent()} == {"user action", "download url=x"}


def test_blank_messages_are_dropped():
    activity_store.append(message="   ", level="info", source=Source.ACTIVITY)
    activity_store.append(message="", level="info", source=Source.ACTIVITY)
    assert activity_store.recent() == []


def test_survives_reopen(tmp_path):
    """The whole point of #33: events persist across a restart (a fresh
    connection to the same file still sees them)."""
    db = tmp_path / "persist.db"
    activity_store.init(db)
    activity_store.clear()
    activity_store.append(message="before restart", level="warning", source=Source.AUDIT)

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


def test_clear_empties_the_store():
    activity_store.append(message="x", level="info", source=Source.ACTIVITY)
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
    activity_store.append(message="x", level="info", source=Source.ACTIVITY)
    assert activity_store.SCHEMA_VERSION >= 1
    assert _user_version(db) == activity_store.SCHEMA_VERSION


def test_downgrade_guard_falls_back_to_memory_without_crashing(tmp_path):
    """A DB stamped newer than this build understands (a Harmonist downgrade) must
    not be written to or crash startup — the store degrades to in-memory."""
    db = tmp_path / "future.db"
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute(f"PRAGMA user_version = {activity_store.SCHEMA_VERSION + 5}")
    conn.close()

    activity_store.init(db)  # must not raise
    activity_store.append(message="still works", level="info", source=Source.ACTIVITY)
    assert [e.message for e in activity_store.recent()] == ["still works"]

    # The future-versioned file was left untouched — no events table written to it.
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "events" not in tables
