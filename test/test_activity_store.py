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


def test_audit_paths_are_recorded_relative_to_the_library(tmp_path):
    """#98: an absolute path is mostly noise — under Docker the prefix is a
    container path that means nothing to the user, and the part identifying the
    album is the tail. Relativised centrally in `_fmt`, so the writers (sidecar,
    tagger, cover_art, bandcamp_hook) never need to know where the library is."""
    from pathlib import Path

    music = tmp_path / "music"
    audit.set_library_root(music)

    audit.record("tag.album", album=music / "Artist" / "Album")
    assert (
        activity_store.recent(1, source=Source.AUDIT)[0].message == "tag.album album=Artist/Album"
    )

    # Outside the library, absolute is the honest rendering.
    audit.record("checkpoint.clear", path=Path("/etc/elsewhere.json"))
    assert (
        activity_store.recent(1, source=Source.AUDIT)[0].message
        == "checkpoint.clear path=/etc/elsewhere.json"
    )

    # The root itself has no meaningful relative form, so it stays absolute
    # rather than rendering as a bare ".".
    audit.record("scan", dir=music)
    assert str(music) in activity_store.recent(1, source=Source.AUDIT)[0].message

    # Strings are left alone — guessing which ones are paths would risk
    # mangling ordinary field values.
    audit.record("note", detail="/music/not-a-path-field")
    assert "/music/not-a-path-field" in activity_store.recent(1, source=Source.AUDIT)[0].message


def test_audit_paths_stay_absolute_before_a_library_root_is_set():
    """Default is unset, so anything recorded before startup wiring — or in a
    test — renders exactly as it always did."""
    from pathlib import Path

    audit.set_library_root(None)
    audit.record("move", src=Path("/music/a.m4a"))
    assert activity_store.recent(1, source=Source.AUDIT)[0].message == "move src=/music/a.m4a"


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


def test_demo_mode_uses_an_in_memory_store_and_writes_no_file(tmp_path, monkeypatch):
    """#69: demo mode shares the REAL config dir (only the music dir is
    sandboxed), so a file-backed store would append demo events to the user's
    genuine history — and re-open a DB the demo has no business touching. It
    must stay in memory and leave no file behind."""
    from harmonist.config import BandcampConfig, Config, PathsConfig, ServerConfig, TestConfig
    from harmonist.web.main import create_app

    cfg = Config(
        paths=PathsConfig(config_dir=tmp_path / "config", music_dir=tmp_path / "music"),
        bandcamp=BandcampConfig(),
        server=ServerConfig(),
        test=TestConfig(mode="fixture"),
        demo_mode=True,
    )
    cfg.paths.config_dir.mkdir(parents=True)
    cfg.paths.music_dir.mkdir(parents=True)

    create_app(cfg)
    activity.record("demo-only event")

    assert not (cfg.paths.config_dir / "activity.db").exists()
    # ...but the feed still works in-process.
    assert "demo-only event" in [e.message for e in activity_store.recent()]


def test_non_demo_mode_still_writes_the_file(tmp_path):
    """The counterpart: a normal run must still persist to activity.db."""
    from harmonist.config import BandcampConfig, Config, PathsConfig, ServerConfig, TestConfig
    from harmonist.web.main import create_app

    cfg = Config(
        paths=PathsConfig(config_dir=tmp_path / "config", music_dir=tmp_path / "music"),
        bandcamp=BandcampConfig(),
        server=ServerConfig(),
        test=TestConfig(mode="fixture"),
    )
    cfg.paths.config_dir.mkdir(parents=True)
    cfg.paths.music_dir.mkdir(parents=True)

    create_app(cfg)
    activity.record("persisted event")

    assert (cfg.paths.config_dir / "activity.db").exists()


# ---------- album identity aliases (#33) ----------


def test_migrates_a_v3_database_in_place_keeping_its_rows(tmp_path):
    """The 3 -> 4 upgrade (album_aliases): a v3 activity.db gains the table in
    place, keeps every existing row, and starts empty of aliases. Built from a
    PREFIX of the real _MIGRATIONS so it can't drift from what shipped."""
    db = tmp_path / "v3.db"
    conn = sqlite3.connect(db, isolation_level=None)
    for step in activity_store._MIGRATIONS[:3]:
        for stmt in step:
            conn.execute(stmt)
    conn.execute(
        "INSERT INTO events (ts, level, source, message, album_id) VALUES (?, ?, ?, ?, ?)",
        ("2026-07-01T00:00:00+00:00", "info", "activity", "written by v3", "rel-old"),
    )
    conn.execute("PRAGMA user_version = 3")
    conn.close()

    activity_store.init(db)

    assert _user_version(db) == activity_store.SCHEMA_VERSION
    assert [e.message for e in activity_store.recent()] == ["written by v3"]
    assert activity_store.resolve_alias("rel-old") is None  # no aliases yet
    # ...and the upgraded DB accepts them.
    activity_store.record_alias("rel-old", "rel-new")
    assert activity_store.resolve_alias("rel-old") == "rel-new"


def test_resolve_alias_follows_a_chain_and_survives_a_cycle(tmp_path):
    """Identity can move more than once (temp_uid -> mbid -> corrected mbid), so
    resolution is transitive. A cycle must terminate rather than hang a request."""
    activity_store.init(tmp_path / "a.db")
    activity_store.record_alias("uid-1", "mbid-a")
    activity_store.record_alias("mbid-a", "mbid-b")
    assert activity_store.resolve_alias("uid-1") == "mbid-b"
    assert activity_store.resolve_alias("mbid-a") == "mbid-b"
    assert activity_store.resolve_alias("mbid-b") is None  # current; nothing beyond
    assert activity_store.resolve_alias("never-seen") is None

    # A -> B -> A must not spin.
    activity_store.record_alias("mbid-b", "uid-1")
    assert activity_store.resolve_alias("uid-1") is not None  # terminates


def test_clear_drops_aliases_too(tmp_path):
    """A demo re-seed (or the next test) must not inherit aliases pointing at
    albums that no longer exist — they could mis-resolve a deep link."""
    activity_store.init(tmp_path / "a.db")
    activity_store.record_alias("old", "new")
    activity_store.clear()
    assert activity_store.resolve_alias("old") is None


def test_sidecar_write_records_the_identity_change(tmp_path):
    """Capture happens at the sidecar write — the only moment the pair is
    knowable, since normalisation erases temp_uid once an MBID lands."""
    from harmonist import sidecar as sc
    from harmonist.models import Sidecar

    activity_store.init(tmp_path / "a.db")
    album = tmp_path / "album"
    album.mkdir()

    sc.write(album, Sidecar(store_url="https://x.bandcamp.com/album/y"))
    uid = sc.read(album).temp_uid
    assert uid

    # Tagging moves the identity to the MBID and erases temp_uid...
    sc.write(album, Sidecar(store_url="https://x.bandcamp.com/album/y", mb_release_id="rel-1"))
    assert sc.read(album).temp_uid is None
    # ...but the link survives in the alias table.
    assert activity_store.resolve_alias(uid) == "rel-1"

    # A re-match (MBID -> MBID) is captured too, and chains.
    sc.write(album, Sidecar(store_url="https://x.bandcamp.com/album/y", mb_release_id="rel-2"))
    assert activity_store.resolve_alias("rel-1") == "rel-2"
    assert activity_store.resolve_alias(uid) == "rel-2"


def test_sidecar_create_and_noop_rewrite_record_no_alias(tmp_path):
    """A create supersedes nothing, and re-writing an unchanged identity must not
    manufacture a self-alias."""
    from harmonist import sidecar as sc
    from harmonist.models import Sidecar

    activity_store.init(tmp_path / "a.db")
    album = tmp_path / "album"
    album.mkdir()

    sc.write(album, Sidecar(mb_release_id="rel-1"))
    assert activity_store.resolve_alias("rel-1") is None
    sc.write(album, Sidecar(mb_release_id="rel-1", notes="changed something else"))
    assert activity_store.resolve_alias("rel-1") is None


# ---------- action correlation (#84) ----------


def test_migrates_a_v4_database_in_place_keeping_its_rows(tmp_path):
    """4 -> 5 (action_id): gains the column in place, keeps existing rows, and
    reads NULL for them — they predate it, so there is nothing to back-fill.
    Built from a PREFIX of the real _MIGRATIONS so it can't drift from what
    shipped."""
    db = tmp_path / "v4.db"
    conn = sqlite3.connect(db, isolation_level=None)
    for step in activity_store._MIGRATIONS[:4]:
        for stmt in step:
            conn.execute(stmt)
    conn.execute(
        "INSERT INTO events (ts, level, source, message, album_id) VALUES (?, ?, ?, ?, ?)",
        ("2026-07-01T00:00:00+00:00", "info", "activity", "written by v4", "rel-old"),
    )
    conn.execute("PRAGMA user_version = 4")
    conn.close()

    activity_store.init(db)

    assert _user_version(db) == activity_store.SCHEMA_VERSION
    events = activity_store.recent()
    assert [e.message for e in events] == ["written by v4"]
    assert events[0].album_id == "rel-old"  # pre-existing data intact
    assert events[0].action_id is None


def test_migrates_a_v5_database_in_place_keeping_its_rows(tmp_path):
    """5 -> 6 (tag_changes): gains the side table in place and keeps existing
    rows. Nothing is back-filled — records written before this migration have no
    per-field detail, and inventing one would be worse than admitting that."""
    db = tmp_path / "v5.db"
    conn = sqlite3.connect(db, isolation_level=None)
    for step in activity_store._MIGRATIONS[:5]:
        for stmt in step:
            conn.execute(stmt)
    conn.execute(
        "INSERT INTO events (ts, level, source, message, album_id) VALUES (?, ?, ?, ?, ?)",
        ("2026-07-01T00:00:00+00:00", "info", "audit", "tag.track file=01.flac", "rel-old"),
    )
    conn.execute("PRAGMA user_version = 5")
    conn.close()

    activity_store.init(db)

    assert _user_version(db) == activity_store.SCHEMA_VERSION
    events = activity_store.recent()
    assert [e.message for e in events] == ["tag.track file=01.flac"]
    assert events[0].album_id == "rel-old"  # pre-existing data intact
    # The old row has no detail, and asking for it is not an error.
    assert activity_store.tag_changes_for([1]) == {}


def test_a_failing_migration_leaves_the_database_exactly_as_it_was(tmp_path, monkeypatch):
    """No half-migrated databases. This runs on someone's NAS with no way to
    fix it remotely, so a step that dies part-way must leave nothing behind.

    Three things make it hold, two of which are easy to break by accident:
    SQLite has transactional DDL; `_open` uses isolation_level=None, WITHOUT
    which Python's sqlite3 runs DDL outside the surrounding BEGIN and it would
    survive the ROLLBACK; and `PRAGMA user_version` lives in the database header
    and rolls back too, so the schema and its version can't disagree.
    """
    db = tmp_path / "v5.db"
    conn = sqlite3.connect(db, isolation_level=None)
    for step in activity_store._MIGRATIONS[:5]:
        for stmt in step:
            conn.execute(stmt)
    conn.execute(
        "INSERT INTO events (ts, level, source, message) VALUES (?, ?, ?, ?)",
        ("2026-07-01T00:00:00+00:00", "info", "activity", "precious user data"),
    )
    conn.execute("PRAGMA user_version = 5")
    conn.close()

    # A step whose SECOND statement fails, after the first has already run.
    broken = (
        "CREATE TABLE half_built (id INTEGER PRIMARY KEY)",
        "CREATE INDEX idx_boom ON no_such_table (nope)",
    )
    monkeypatch.setattr(activity_store, "_MIGRATIONS", (*activity_store._MIGRATIONS[:5], broken))
    monkeypatch.setattr(activity_store, "SCHEMA_VERSION", 6)

    conn = sqlite3.connect(db, check_same_thread=False, isolation_level=None)
    with pytest.raises(sqlite3.OperationalError):
        activity_store._migrate(conn)
    conn.close()

    check = sqlite3.connect(db)
    tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    rows = [r[0] for r in check.execute("SELECT message FROM events")]
    (version,) = check.execute("PRAGMA user_version").fetchone()
    check.close()

    assert version == 5, "version advanced past a step that failed"
    assert "half_built" not in tables, "the failed step's first statement survived"
    assert rows == ["precious user data"]


def test_a_database_left_by_a_failed_migration_still_upgrades_later(tmp_path, monkeypatch):
    """The other half of the guarantee: rolling back must leave the database
    upgradeable, not merely intact. A user who hits a bad build should be fixed
    by the next one, with their history still there."""
    db = tmp_path / "v5.db"
    conn = sqlite3.connect(db, isolation_level=None)
    for step in activity_store._MIGRATIONS[:5]:
        for stmt in step:
            conn.execute(stmt)
    conn.execute(
        "INSERT INTO events (ts, level, source, message) VALUES (?, ?, ?, ?)",
        ("2026-07-01T00:00:00+00:00", "info", "activity", "survives a bad build"),
    )
    conn.execute("PRAGMA user_version = 5")
    conn.close()

    broken = ("CREATE INDEX idx_boom ON no_such_table (nope)",)
    with monkeypatch.context() as m:
        m.setattr(activity_store, "_MIGRATIONS", (*activity_store._MIGRATIONS[:5], broken))
        m.setattr(activity_store, "SCHEMA_VERSION", 6)
        bad = sqlite3.connect(db, check_same_thread=False, isolation_level=None)
        with pytest.raises(sqlite3.OperationalError):
            activity_store._migrate(bad)
        bad.close()

    # The real migrations, as a later build would apply them.
    activity_store.init(db)
    assert _user_version(db) == activity_store.SCHEMA_VERSION
    assert [e.message for e in activity_store.recent()] == ["survives a bad build"]


def test_append_returns_the_row_id_it_wrote(tmp_path):
    """The id is what lets a caller attach detail to the row it just wrote. A
    write that produced no row returns None, so the caller can tell."""
    activity_store.init(tmp_path / "a.db")
    first = activity_store.append(
        message="tag.track file=01.flac",
        level=activity_store.Level.INFO,
        source=activity_store.Source.AUDIT,
    )
    second = activity_store.append(
        message="tag.track file=02.flac",
        level=activity_store.Level.INFO,
        source=activity_store.Source.AUDIT,
    )
    assert isinstance(first, int) and isinstance(second, int)
    assert second > first
    # An empty message is not recorded, so there is no row to hang detail off.
    assert (
        activity_store.append(
            message="   ",
            level=activity_store.Level.INFO,
            source=activity_store.Source.AUDIT,
        )
        is None
    )


def test_tag_changes_round_trip_with_every_identifier(tmp_path):
    """All four ways of naming the track survive the round trip — each fails
    under a different future edit (rename, re-match, renumber), so a record that
    kept only one would be unrevertable after that edit."""
    activity_store.init(tmp_path / "a.db")
    event_id = activity_store.append(
        message="tag.track file=01.flac",
        level=activity_store.Level.INFO,
        source=activity_store.Source.AUDIT,
        album_id="rel-1",
    )
    assert event_id is not None
    activity_store.record_tag_changes(
        event_id,
        file="01 Wildlife Analysis.flac",
        changes={
            "artist": ["Boards Of Canada", "Boards of Canada"],
            "label": [None, "Warp Records"],
            "isrcs": [[], ["GBAAA9800001"]],
        },
        track_ref="rt-1",
        rec_ref="rec-1",
        position="1",
    )

    got = activity_store.tag_changes_for([event_id])[event_id]
    assert got.file == "01 Wildlife Analysis.flac"
    assert got.track_ref == "rt-1"
    assert got.rec_ref == "rec-1"
    assert got.position == "1"
    assert got.changes["artist"] == ["Boards Of Canada", "Boards of Canada"]
    # Absent-before round-trips as null, distinct from an empty string.
    assert got.changes["label"] == [None, "Warp Records"]
    # Multi-valued fields stay lists rather than being flattened at write time.
    assert got.changes["isrcs"] == [[], ["GBAAA9800001"]]


def test_tag_changes_records_nothing_when_nothing_changed(tmp_path):
    """A file whose tags are identical writes no row. Otherwise a nightly
    gardener run (#32) fills history with empty entries."""
    activity_store.init(tmp_path / "a.db")
    event_id = activity_store.append(
        message="tag.track file=01.flac",
        level=activity_store.Level.INFO,
        source=activity_store.Source.AUDIT,
    )
    assert event_id is not None
    activity_store.record_tag_changes(event_id, file="01.flac", changes={})
    assert activity_store.tag_changes_for([event_id]) == {}


def test_tag_changes_skips_an_unreadable_payload_without_losing_the_rest(tmp_path):
    """These records are permanent and unversioned, so one row written by a
    future (or corrupted) build must not take a whole album page down."""
    db = tmp_path / "a.db"
    activity_store.init(db)
    good = activity_store.append(
        message="tag.track file=01.flac",
        level=activity_store.Level.INFO,
        source=activity_store.Source.AUDIT,
    )
    bad = activity_store.append(
        message="tag.track file=02.flac",
        level=activity_store.Level.INFO,
        source=activity_store.Source.AUDIT,
    )
    assert good is not None and bad is not None
    activity_store.record_tag_changes(good, file="01.flac", changes={"title": ["a", "b"]})

    conn = activity_store._ensure()
    conn.execute(
        "INSERT INTO tag_changes (event_id, file, changes) VALUES (?, ?, ?)",
        (bad, "02.flac", "{not json"),
    )
    conn.commit()

    out = activity_store.tag_changes_for([good, bad])
    assert set(out) == {good}
    assert out[good].changes == {"title": ["a", "b"]}


def test_action_scope_correlates_activity_with_its_audit_records(tmp_path):
    """The whole point: one action's activity entry and the audit records
    underneath it share an id, and unscoped events stay uncorrelated."""
    from harmonist import audit

    activity_store.init(tmp_path / "a.db")
    with activity_store.action() as aid:
        activity.record("Re-tagged", album_id="rel-1")
        audit.record("sidecar.update", album_id="rel-1", mbid="old->new")
        audit.record("move", src="/a", dst="/b")
    activity.record("Unrelated, outside any action")

    detail = activity_store.audit_by_action([aid])
    # album_id is a structured column, not part of the message (see audit.record).
    assert [e.message for e in detail[aid]] == [
        "sidecar.update mbid=old->new",
        "move src=/a dst=/b",
    ]
    assert [e.album_id for e in detail[aid]] == ["rel-1", None]
    outside = next(e for e in activity_store.recent() if "Unrelated" in e.message)
    assert outside.action_id is None


def test_audit_by_action_groups_and_ignores_activity_rows(tmp_path):
    """Grouping is by action, and only AUDIT rows come back — the activity entry
    is the thing being annotated, not part of the annotation."""
    from harmonist import audit

    activity_store.init(tmp_path / "a.db")
    with activity_store.action() as a1:
        activity.record("first")
        audit.record("one")
    with activity_store.action() as a2:
        activity.record("second")
        audit.record("two")

    got = activity_store.audit_by_action([a1, a2])
    assert {k: [e.message for e in v] for k, v in got.items()} == {a1: ["one"], a2: ["two"]}
    assert activity_store.audit_by_action([]) == {}


def test_action_scopes_are_isolated_between_threads(tmp_path):
    """The sync and reconcile runners are plain Threads. They must NOT inherit a
    request's action id — each opens its own — or a background write would be
    filed under whatever request happened to be in flight."""
    import threading

    activity_store.init(tmp_path / "a.db")
    seen: dict[str, str | None] = {}

    def worker() -> None:
        seen["inherited"] = activity_store.current_action()
        with activity_store.action() as own:
            seen["own"] = own

    with activity_store.action() as outer:
        t = threading.Thread(target=worker)
        t.start()
        t.join()

    assert seen["inherited"] is None  # no leakage into the thread
    assert seen["own"] != outer  # it minted its own


def test_nested_action_scopes_keep_the_outermost_id(tmp_path):
    """A helper that opens its own scope inside a request must not fragment that
    request's records across two ids."""
    activity_store.init(tmp_path / "a.db")
    with activity_store.action() as outer:
        with activity_store.action() as inner:
            assert inner == outer
        assert activity_store.current_action() == outer
    assert activity_store.current_action() is None


# ---------- A broken store must not look like an empty one (#104) ----------


@pytest.fixture
def broken_store(monkeypatch):
    """Make every store operation fail the way a locked/corrupt DB would.

    Patching `_ensure` is enough: every read and write calls it from inside the
    same `try`, so the error surfaces exactly where a real sqlite failure would.
    """

    def boom():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(activity_store, "_ensure", boom)


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda: activity_store.recent(), id="recent"),
        pytest.param(lambda: activity_store.audit_by_action(["a"]), id="audit_by_action"),
        pytest.param(lambda: activity_store.resolve_alias("some-id"), id="resolve_alias"),
        pytest.param(lambda: activity_store.album_history("some-id"), id="album_history"),
    ],
)
def test_reads_raise_rather_than_returning_an_empty_result(broken_store, call):
    """Every read used to swallow sqlite3.Error and return []/{}/None, which the
    caller cannot tell from a genuine empty answer — the album page then claims
    "Nothing recorded for this album yet" over a broken database (#104)."""
    with pytest.raises(activity_store.StoreUnavailableError):
        call()


def test_resolve_alias_failure_is_not_reported_as_never_superseded(broken_store):
    """The sharpest case: None from resolve_alias means "this id was never
    superseded", which sends its only caller on to a 404. A DB error must not be
    able to say that about an album sitting on disk."""
    with pytest.raises(activity_store.StoreUnavailableError):
        activity_store.resolve_alias("old-id")


def test_writes_stay_best_effort_but_log_at_error(broken_store, caplog):
    """Recording must never abort the operation being recorded — but a dropped
    audit row makes every remaining row untrustworthy, so it can't be quiet.
    These were log.debug, i.e. invisible at default level AND below the feed
    mirror's WARNING threshold (#104, skill §5)."""
    caplog.set_level("DEBUG")
    activity_store.append(message="x", level=Level.INFO, source=Source.AUDIT)
    activity_store.record_alias("old", "new")
    activity_store.clear()  # teardown path — swallows too, but must be loud

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 3, [r.message for r in caplog.records]
    assert all(r.exc_info for r in errors), "no traceback — the thing you want in 3 weeks"


def test_append_failure_does_not_re_enter_the_feed_mirror(broken_store, caplog):
    """The `_activity` flag on the failure log is load-bearing: without it the
    feed's WARNING+ mirror would call record() again, which fails again..."""
    caplog.set_level("DEBUG")
    activity_store.append(message="x", level=Level.INFO, source=Source.ACTIVITY)
    failures = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(failures) == 1
    assert getattr(failures[0], "_activity", False) is True
