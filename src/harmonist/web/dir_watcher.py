"""Filesystem watcher that triggers a library rescan on external changes.

Files added or removed outside the app (copied straight into the music dir,
deleted by hand) don't pass through the rescan-after-mutation middleware, so
without this they'd stay invisible until the next in-app action. A `watchfiles`
watcher bridges that gap: it calls `on_change` (wired to
`ScanRunner.request_scan`) whenever the tree changes, and the scanner's
per-album mtime cache keeps the resulting rescan cheap.

Caveat — inotify only fires for *local* filesystem changes. On a network mount
(SMB/NFS) the kernel never delivers events, so the watcher silently sees
nothing; restarting the container forces a fresh scan there. The watcher fails
soft: any setup error (missing dir, watch-limit exhaustion) is logged and the
task exits without taking the app down. See docs/design.md §10.4.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from watchfiles import awatch

log = logging.getLogger(__name__)


async def watch_music_dir(
    music_dir: Path,
    on_change: Callable[[], None],
    *,
    settle_seconds: float = 5.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Rescan once `music_dir` has been quiet for `settle_seconds`.

    Each change (re)starts a settle timer; the rescan fires only after the tree
    has been idle for the full delay, so copying a batch of files in produces a
    single scan instead of scanning mid-copy. Runs until cancelled or
    `stop_event` is set; never raises into the caller (failures are logged and
    end the task)."""
    if not music_dir.is_dir():
        log.warning("File watcher disabled: %s is not a directory", music_dir)
        return

    loop = asyncio.get_running_loop()
    pending: asyncio.TimerHandle | None = None

    def fire() -> None:
        log.info("Library settled after change — requesting rescan")
        on_change()

    log.info(
        "Watching %s for changes (auto-rescan %.0fs after activity settles)",
        music_dir,
        settle_seconds,
    )
    try:
        # Small awatch debounce so the settle timer closely tracks real activity;
        # the user-facing quiescence delay is `settle_seconds`, enforced below.
        async for _changes in awatch(music_dir, stop_event=stop_event, debounce=200):
            log.debug("Change detected; restarting %.0fs settle timer", settle_seconds)
            if pending is not None:
                pending.cancel()
            pending = loop.call_later(settle_seconds, fire)
    except asyncio.CancelledError:
        raise  # normal shutdown — let it propagate
    except Exception:
        # e.g. inotify watch-limit exhaustion on a large tree, or an unsupported
        # filesystem. Degrade to no-op rather than crash the app.
        log.exception("File watcher stopped; manual edits won't auto-rescan")
    finally:
        if pending is not None:
            pending.cancel()
