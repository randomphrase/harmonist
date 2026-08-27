"""The interval task behind the hourly library rescan (web/periodic.py).

Its whole job is "call this every N seconds until told to stop", and what
matters is what a NAS running unattended for months depends on: a tick that
raises does not silently retire the loop, and a loop that keeps failing does
not keep saying so.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import pytest

from harmonist.web.periodic import run_periodically


def test_a_non_positive_interval_raises_rather_than_spinning():
    """`wait_for(..., timeout=0)` returns instantly, so a zero interval would
    turn this into a busy loop calling the action as fast as the machine
    allows. No caller legitimately wants that, so it is a programming error and
    must say so rather than quietly becoming a no-op or a spin."""
    calls: list[int] = []

    async def go() -> None:
        await asyncio.wait_for(
            run_periodically(timedelta(0), lambda: calls.append(1), name="test"), timeout=1
        )

    with pytest.raises(ValueError, match="must be positive"):
        asyncio.run(go())
    assert calls == []


def test_fires_on_the_interval_until_stopped():
    """Ticks repeat, and setting the stop event ends the loop rather than
    leaving a task the lifespan has to cancel out from under a running action."""
    stop = asyncio.Event()
    calls: list[int] = []

    def tick() -> None:
        calls.append(1)
        if len(calls) == 3:
            stop.set()

    async def go() -> None:
        await asyncio.wait_for(
            run_periodically(timedelta(seconds=0.01), tick, name="test", stop_event=stop), timeout=5
        )

    asyncio.run(go())
    assert len(calls) == 3


def test_a_failing_tick_does_not_end_the_loop():
    """One transient failure — an unmounted volume, a moment of EIO — must not
    retire the rescan, or the library stays stale until the next restart."""
    stop = asyncio.Event()
    calls: list[int] = []

    def tick() -> None:
        calls.append(1)
        if len(calls) == 3:
            stop.set()
            return
        raise OSError("volume went away")

    async def go() -> None:
        await asyncio.wait_for(
            run_periodically(timedelta(seconds=0.01), tick, name="test", stop_event=stop), timeout=5
        )

    asyncio.run(go())
    assert len(calls) == 3  # it kept going after two raising ticks


def test_a_persistent_failure_is_reported_once_not_every_tick(caplog):
    """WARNING+ on the `harmonist` logger is mirrored into the user's Activity
    feed, so a condition that doesn't clear on its own — an unmounted volume —
    must not post an identical entry there every interval until someone
    notices. One entry per failure episode; recovery says so at INFO, which the
    feed doesn't mirror."""
    caplog.set_level("DEBUG", logger="harmonist.web.periodic")
    stop = asyncio.Event()
    calls: list[int] = []

    def tick() -> None:
        calls.append(1)
        if len(calls) == 4:
            stop.set()
            return  # the fourth tick succeeds
        raise OSError("volume went away")

    async def go() -> None:
        await asyncio.wait_for(
            run_periodically(timedelta(seconds=0.01), tick, name="test", stop_event=stop), timeout=5
        )

    asyncio.run(go())
    # Filtered by logger: once an app has been built in this process, the
    # activity mirror is attached to `harmonist` and re-logs every WARNING it
    # sees — which is the very behaviour this test exists to keep quiet.
    mine = [r for r in caplog.records if r.name == "harmonist.web.periodic"]
    loud = [r for r in mine if r.levelno >= logging.WARNING]
    assert len(loud) == 1, [r.message for r in loud]
    assert any("recovered after 3" in r.message for r in mine)
