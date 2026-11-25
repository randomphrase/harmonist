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
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any

import httpx

from harmonist.artist_store import ArtistStore
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
        self.store = ArtistStore()
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

    async def write[T](
        self,
        path: Path,
        fn: Callable[[], T],
        *,
        prepare: Callable[[], Awaitable[None]] | None = None,
    ) -> T:
        """Resolve aliases off-loop, then accept preparation and writing together.

        Async preparation records completed lookup observations without occupying
        a file worker. It has the same cancellation protection as the file write.
        """
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
            task = asyncio.create_task(self._complete(fn, lock, prepare))
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

    async def _complete[T](
        self,
        fn: Callable[[], T],
        lock: asyncio.Lock,
        prepare: Callable[[], Awaitable[None]] | None,
    ) -> T:
        try:
            if prepare is not None:
                await prepare()
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

    def stop(self) -> None:
        """Stop admissions synchronously with the other application jobs."""
        self._stopping = True
        for task in self._lookups | self._waiting:
            task.cancel()

    async def close(self) -> None:
        self.stop()
        pending = self._lookups | self._waiting
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._accepted:
            await asyncio.gather(*self._accepted, return_exceptions=True)
        # Running sync lookups retain their slots until they actually stop;
        # don't close their event loop while they can still record a result.
        await self.musicbrainz.close()
        await self.io.close()
        await self._writes.close()
        await self.store.close()
        await self.http.aclose()
