"""The async artist path shares the durable or ephemeral store with sync writers."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from unittest.mock import Mock

import pytest

from harmonist import activity_store, mb_cache, mb_lookup
from harmonist.artist_store import ArtistStore
from harmonist.async_workers import Workers


@pytest.mark.parametrize("database", ["file", "memory", "memory-name", "fallback"])
def test_artist_connections_share_the_initialized_store(tmp_path, monkeypatch, database):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "activity.db"
    if database == "fallback":
        with sqlite3.connect(path) as db:
            db.execute(f"PRAGMA user_version = {activity_store.SCHEMA_VERSION + 1}")
    if database == "memory":
        activity_store.init_memory()
    elif database == "memory-name":
        activity_store.init(":memory:")
        assert not (tmp_path / ":memory:").exists()
    else:
        activity_store.init(path)
    activity_store.store_artist("artist", {"name": "Before"})
    activity_store.store_artist_biography("artist", {"status": "missing"})

    async def run():
        store = ArtistStore()
        try:
            await store.store_artist("artist", {"name": "After"})
            await store.keep_lookups("artist", biography=None, picture={"status": "found"})
            saved = activity_store.cached_artist("artist")
            assert saved is not None and saved.payload == {"name": "After"}
            assert saved.biography == {"status": "missing"}
            assert saved.picture == {"status": "found"}
            activity_store.store_artist("artist", {"name": "Sync writer"})
            conn = await store._connect()
            async with conn.execute(
                "SELECT payload FROM mb_artist_cache WHERE mbid = 'artist'"
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None and json.loads(row[0]) == {"name": "Sync writer"}
        finally:
            await store.close()

    asyncio.run(run())
    saved = activity_store.cached_artist("artist")
    assert saved is not None and saved.payload == {"name": "Sync writer"}
    if database in {"memory", "memory-name"}:
        assert not path.exists()
        activity_store.init_memory()
        assert activity_store.cached_artist("artist") is None
    elif database == "fallback":
        with sqlite3.connect(path) as db:
            assert (
                db.execute("PRAGMA user_version").fetchone()[0] == activity_store.SCHEMA_VERSION + 1
            )
            assert (
                db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall() == []
            )
    else:
        activity_store.init(path)
        saved = activity_store.cached_artist("artist")
        assert saved is not None and saved.payload == {"name": "Sync writer"}


def test_async_artist_fetch_is_live_and_stores_the_returned_id(tmp_path, monkeypatch):
    activity_store.init(tmp_path / "activity.db")
    activity_store.store_artist("old", {"id": "old", "name": "Stale"})
    read = Mock(return_value={"id": "merged", "name": "Live"})
    monkeypatch.setattr(mb_lookup, "fetch_artist", read)

    async def run():
        workers = Workers(1, "test-mb")
        store = ArtistStore()
        try:
            assert await mb_cache.fetch_artist_async("old", workers, store) == read.return_value
            merged = activity_store.cached_artist("merged")
            assert merged is not None and merged.payload == read.return_value
            previous = activity_store.cached_artist("old")
            assert previous is not None and previous.payload["name"] == "Stale"
            read.side_effect = mb_lookup.MBError("unavailable")
            with pytest.raises(mb_lookup.MBError, match="unavailable"):
                await mb_cache.fetch_artist_async("missing", workers, store)
            assert activity_store.cached_artist("missing") is None
        finally:
            await workers.close()
            await store.close()

    asyncio.run(run())
    assert read.call_count == 2


def test_async_store_connection_failure_is_loud_and_does_not_fail_the_lookup(
    tmp_path, monkeypatch, caplog
):
    activity_store.init(tmp_path / "activity.db")
    monkeypatch.setattr(mb_lookup, "fetch_artist", Mock(return_value={"id": "artist"}))
    monkeypatch.setattr(
        activity_store,
        "open_artist_connection",
        Mock(side_effect=sqlite3.OperationalError("locked")),
    )

    async def run():
        workers = Workers(1, "test-mb")
        store = ArtistStore()
        try:
            assert await mb_cache.fetch_artist_async("artist", workers, store) == {"id": "artist"}
            await store.keep_lookups("artist", biography={"status": "missing"}, picture=None)
        finally:
            await workers.close()
            await store.close()

    asyncio.run(run())
    assert activity_store.cached_artist("artist") is None
    assert "artist_store store_artist(artist) failed" in caplog.text
    assert "artist_store biography lookup(artist) failed" in caplog.text
