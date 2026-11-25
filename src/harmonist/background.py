"""Bounded application-owned jobs, with cooperative album boundaries.

Stopping prevents admissions, not completion. A worker already inside an album
keeps its action context through recording and publication; the next album is
never accepted after stop. Synchronous calls retain their worker until return.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor

log = logging.getLogger(__name__)
_stop: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar(
    "background_stop", default=None
)


def stopping() -> bool:
    event = _stop.get()
    return event is not None and event.is_set()


def items[T](values: Iterable[T]) -> Iterator[T]:
    """Accept one complete album operation at a time, until shutdown."""
    for value in values:
        if stopping():
            return
        yield value


class Jobs:
    def __init__(self, *, workers: int = 4) -> None:
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="harmonist-job")
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._active: set[str] = set()
        self._again: dict[str, Callable[[], object]] = {}
        self._running: set[Future[None]] = set()
        self._drain: asyncio.Task[None] | None = None

    def start(
        self,
        name: str,
        fn: Callable[[], object],
        *,
        repeat: bool = False,
    ) -> bool:
        """Admit at most one running and one coalesced repeat per job name.

        Callers that aggregate changed albums retain them by key; the repeat
        merely wakes their drain. Independent passes start without request IDs.
        """
        with self._lock:
            if self._stop.is_set():
                return False
            if name in self._active:
                if repeat:
                    self._again[name] = fn
                return repeat
            context = contextvars.Context()
            context.run(_stop.set, self._stop)
            self._active.add(name)
            try:
                future = self._pool.submit(context.run, self._run, name, fn)
            except BaseException:
                self._active.remove(name)
                raise
            self._running.add(future)
        future.add_done_callback(self._finished)
        return True

    def _run(self, name: str, fn: Callable[[], object]) -> None:
        while True:
            try:
                fn()
            except Exception:
                # Application job boundary: a defect must be visible, and must
                # not strand admission or silently retire subsequent passes.
                log.exception("background %s failed", name)
            except BaseException:
                # Release admission on process-level interruption as well;
                # unlike an ordinary job failure, it still propagates.
                with self._lock:
                    self._active.remove(name)
                raise
            with self._lock:
                again = self._again.pop(name, None)
                if again is None or self._stop.is_set():
                    self._active.remove(name)
                    return
                fn = again

    def _finished(self, future: Future[None]) -> None:
        with self._lock:
            self._running.discard(future)
        # Normal job errors are reported at the boundary above. Retrieve any
        # BaseException too, so the future never becomes an unobserved failure.
        if not future.cancelled() and (error := future.exception()) is not None:
            log.error("background worker terminated", exc_info=error)

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            self._again.clear()

    def running(self, name: str) -> bool:
        with self._lock:
            return name in self._active

    async def close(self) -> None:
        self.stop()
        if self._drain is not None and self._drain.done():
            self._drain.result()
            return
        if self._drain is None:
            self._drain = asyncio.create_task(self._close())
        await asyncio.shield(self._drain)

    async def _close(self) -> None:
        await self.wait_idle()
        self._pool.shutdown(wait=False)

    async def wait_idle(self) -> None:
        """Wait for admitted jobs and their coalesced work to finish."""
        while True:
            with self._lock:
                running = tuple(self._running)
            if not running:
                return
            await asyncio.gather(
                *(asyncio.wrap_future(future) for future in running), return_exceptions=True
            )
