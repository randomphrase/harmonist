"""Bounded off-loop work whose capacity follows the thread, not its waiter."""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any


class Workers:
    """Submit only after a slot is free; cancellation cannot free a running slot.

    Each instance belongs to one event loop and is drained before that loop
    closes. Copy the submitting action's context into every worker.
    """

    def __init__(self, count: int, name: str) -> None:
        self._pool = ThreadPoolExecutor(max_workers=count, thread_name_prefix=name)
        self._slots = asyncio.Semaphore(count)
        self._running: set[asyncio.Future[Any]] = set()
        self._closed = False

    async def run[T](self, fn: Callable[[], T]) -> T:
        await self._slots.acquire()
        if self._closed:
            self._slots.release()
            raise RuntimeError("worker pool is closed")
        context = contextvars.copy_context()
        try:
            future = asyncio.get_running_loop().run_in_executor(self._pool, context.run, fn)
        except BaseException:
            self._slots.release()
            raise
        self._running.add(future)
        future.add_done_callback(self._finished)
        return await asyncio.shield(future)

    def _finished(self, future: asyncio.Future[Any]) -> None:
        self._running.discard(future)
        self._slots.release()
        # A cancelled caller no longer retrieves an eventual worker exception.
        # Consume it here; the active caller still receives it through shield.
        if not future.cancelled():
            future.exception()

    async def close(self) -> None:
        self._closed = True
        if self._running:
            await asyncio.gather(*self._running, return_exceptions=True)
        self._pool.shutdown(wait=False)
