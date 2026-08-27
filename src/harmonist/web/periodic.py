"""A background task that fires an action on a fixed interval.

The file watcher only reports changes that pass through the local kernel's VFS,
so it is blind to a library edited from another machine over NFS/SMB (#152) and
to its own death — an exhausted inotify watch limit ends the watcher, and from
then on nothing rescans until a restart. Neither is common; both are silent,
which is what makes a periodic rescan worth its cost.

Deliberately generic and deliberately dumb. It owns no state, decides nothing
about what a tick means, and never lets a failing tick end the loop — the whole
of its job is "call this every N seconds until told to stop". The library
rescan is its first caller; the metadata gardener's paced MusicBrainz pass
(#270) is meant to be its second, rather than inventing a second timer pattern
beside it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import timedelta

log = logging.getLogger(__name__)


async def run_periodically(
    interval: timedelta,
    action: Callable[[], object],
    *,
    name: str,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Call `action` every `interval` until cancelled or `stop_event` is set.
    `name` labels the task in the log.

    A `timedelta` rather than a number, so a caller cannot pass the right
    figure in the wrong unit — the one mistake a bare `interval_seconds: float`
    invites and cannot catch.

    The first call happens after one full interval, never at startup: whoever
    engages this has just done the work themselves.

    A non-positive interval raises rather than quietly disabling the task or —
    much worse — spinning: `wait_for(..., timeout=0)` returns instantly, so a
    zero would turn this into a busy loop calling `action` as fast as the
    machine allows. There is no caller for whom that is a legitimate request,
    so it is a programming error and says so.

    A failing tick is logged and the loop continues. This runs unattended for
    months on someone's NAS, so a single transient error — an unmounted volume,
    a moment of EIO — must not silently retire the task and leave it dead for
    the life of the process.

    But only the FIRST of a run of failures is logged loudly. Anything the
    `harmonist` logger emits at WARNING or above is mirrored into the user's
    Activity feed, and a condition that doesn't clear on its own — a volume
    that stays unmounted — would otherwise write the same line into that feed
    every interval until someone noticed. One entry per failure *episode* says
    the same thing; recovery is logged at INFO, which the feed doesn't mirror.
    """
    if interval <= timedelta(0):
        raise ValueError(f"periodic {name}: interval must be positive, got {interval}")
    seconds = interval.total_seconds()
    stop = stop_event if stop_event is not None else asyncio.Event()
    log.info("Periodic %s every %s", name, interval)
    failures = 0
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
            return  # stop_event set — shutting down
        except TimeoutError:
            pass  # the interval elapsed: that IS the tick
        try:
            action()
        except Exception:
            failures += 1
            if failures == 1:
                log.exception("Periodic %s failed; continuing", name)
            else:
                log.debug("Periodic %s failed again (%d in a row)", name, failures)
        else:
            if failures:
                log.info("Periodic %s recovered after %d failure(s)", name, failures)
            failures = 0
