"""Awaitable artist observations in the existing activity database (#1045).

aiosqlite owns the worker and its queue. Synchronous file writers still record
audit/history through activity_store; the artist runtime drains those writers
and accepted observations before closing this connection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from . import activity_store

log = logging.getLogger(__name__)
# A cache-write failure must not make the log mirror re-enter the same store.
_LOG_ONLY = {"_activity": True}


class ArtistStore:
    """One lazily opened connection, owned and closed by the artist runtime."""

    def __init__(self) -> None:
        self._connection: aiosqlite.Connection | None = None
        self._opening = asyncio.Lock()
        self._closed = False

    async def _connect(self) -> aiosqlite.Connection:
        async with self._opening:
            if self._closed:
                raise RuntimeError("artist store is closed")
            if self._connection is None:
                # The public constructor runs this connector on its own worker,
                # including any lazy in-memory initialization, never on the loop.
                self._connection = await aiosqlite.Connection(
                    activity_store.open_artist_connection, iter_chunk_size=64
                )
            return self._connection

    async def store_artist(self, mbid: str, payload: Mapping[str, Any]) -> None:
        """Keep a live answer under its returned ID, preserving earlier lookup notes."""
        try:
            blob = json.dumps(payload, sort_keys=True)
            conn = await self._connect()
            async with conn.execute(
                activity_store.ARTIST_UPSERT, (mbid, datetime.now(UTC).isoformat(), blob)
            ):
                pass  # isolation_level=None: the statement commits before returning.
        except sqlite3.Error, TypeError, ValueError:
            log.exception("artist_store store_artist(%s) failed", mbid, extra=_LOG_ONLY)

    async def keep_lookups(
        self,
        mbid: str,
        *,
        biography: Mapping[str, Any] | None,
        picture: Mapping[str, Any] | None,
    ) -> None:
        """Remember each completed optional lookup; an absent lookup changes nothing."""
        for sql, lookup, name in (
            (activity_store.ARTIST_BIOGRAPHY_UPDATE, biography, "biography"),
            (activity_store.ARTIST_PICTURE_UPDATE, picture, "picture"),
        ):
            if lookup is None:
                continue
            try:
                blob = json.dumps(dict(lookup), sort_keys=True)
                conn = await self._connect()
                async with conn.execute(sql, (blob, mbid)):
                    pass
            except sqlite3.Error, TypeError, ValueError:
                log.exception("artist_store %s lookup(%s) failed", name, mbid, extra=_LOG_ONLY)

    async def close(self) -> None:
        """Drain aiosqlite's queue after the runtime has drained accepted work."""
        async with self._opening:
            self._closed = True
            if self._connection is not None:
                await self._connection.close()
                self._connection = None
