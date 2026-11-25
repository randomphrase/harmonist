"""Application ownership of artist lookups and accepted writes (#1040).

Lookups and waiting writers are cancellable. Acceptance is the synchronous
handoff after obtaining a path lock and a write slot. From there the app owns
verification, audit/history, Activity and inventory publication as one task,
even when the request that asked for it is cancelled. Shutdown drains that task.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import httpx

from harmonist.async_workers import Workers

log = logging.getLogger(__name__)
LOOKUP_SECONDS = 30.0


class Stopping(Exception):
    """Shutdown has stopped admitting artist operations."""


class ArtistWork:
    def __init__(self) -> None:
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(10.0), follow_redirects=True)
        self.io = Workers(4, "artist-files")
        self.musicbrainz = Workers(2, "artist-musicbrainz")
        self._writes = Workers(2, "artist-write")
        self._write_slots = asyncio.Semaphore(2)
        self._paths: weakref.WeakValueDictionary[Path, asyncio.Lock] = weakref.WeakValueDictionary()
        self._lookups: set[asyncio.Task[Any]] = set()
        self._waiting: set[asyncio.Task[Any]] = set()
        self._accepted: set[asyncio.Task[Any]] = set()
        self._stopping = False

    async def lookup[T](self, fn: Callable[[], Coroutine[Any, Any, T]]) -> T:
        if self._stopping:
            raise Stopping
        task = asyncio.create_task(fn())
        self._lookups.add(task)
        try:
            return await task
        finally:
            self._lookups.discard(task)

    async def write[T](self, path: Path, fn: Callable[[], T]) -> T:
        """Resolve path aliases off-loop before entering the write queue."""
        if self._stopping:
            raise Stopping
        waiter = asyncio.current_task()
        assert waiter is not None
        self._waiting.add(waiter)
        lock: asyncio.Lock | None = None
        acquired = False
        slot = False
        try:
            path = await self.io.run(path.resolve)
            if self._stopping:
                raise Stopping
            lock = self._paths.setdefault(path, asyncio.Lock())
            await lock.acquire()
            acquired = True
            await self._write_slots.acquire()
            slot = True
            if self._stopping:
                raise Stopping
            # Cancellation requested before acceptance must still abandon the
            # proposal, even when acquiring uncontended locks didn't suspend.
            if waiter.cancelling():
                raise asyncio.CancelledError
            task = asyncio.create_task(self._complete(fn, lock))
            self._accepted.add(task)
            task.add_done_callback(self._completed)
        except BaseException:
            if slot:
                self._write_slots.release()
            if acquired and lock is not None:
                lock.release()
            raise
        finally:
            self._waiting.discard(waiter)
        return await asyncio.shield(task)

    async def _complete[T](self, fn: Callable[[], T], lock: asyncio.Lock) -> T:
        try:
            return await self._writes.run(fn)
        finally:
            self._write_slots.release()
            lock.release()

    def _completed(self, task: asyncio.Task[Any]) -> None:
        self._accepted.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            # Last-resort boundary: expected write failures are outcomes, but a
            # programming error must remain visible after a request disappears.
            log.error("Accepted artist write failed", exc_info=error)

    async def close(self) -> None:
        self._stopping = True
        pending = self._lookups | self._waiting
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._accepted:
            await asyncio.gather(*self._accepted, return_exceptions=True)
        # Running sync lookups retain their slots until they actually stop;
        # don't close their event loop while they can still record a result.
        await self.musicbrainz.close()
        await self.io.close()
        await self._writes.close()
        await self.http.aclose()
