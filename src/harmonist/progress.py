"""Work the status bar shows while it runs (#757, #786).

A job that works through the library registers a label and its progress here,
and `/status` reports whatever is running, plus the closing line of the last job
to finish. One register rather than a runner, endpoint and status-bar branch per
job: a new bulk job only has to call `job()` to be visible, so the next one is
not forgotten the way the startup update check, the Settings re-check and the
artwork warm-up were.

File writes use the same register with foreground priority and a current file,
so background scans cannot hide the progress of a tag or artwork change.

Not for the routine ten-minute checks. Each tick asks about a handful of albums,
and showing it would flash the bar six times an hour over nothing a person is
waiting for, the noise #349 took out of the log.
"""

from __future__ import annotations

import itertools
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


@dataclass
class Job:
    label: str
    total: int
    done: int = 0
    foreground: bool = False
    item: str | None = None
    #: What the bar says once the job ends, or None for nothing to say. Set by
    #: the job, which knows its result; left unset when it fails, so a failure
    #: never reads as a result.
    closing: str | None = None
    #: The closing line's verb and level, where the label and "info" don't say
    #: them (#1053): one album's write ends "Matched" or "Tagging failed", and a
    #: failure stays in the bar until something replaces it, as an action's does.
    closing_verb: str | None = None
    closing_level: str = "info"
    #: The job this one runs inside, if any (`_enclosing`).
    parent: Job | None = None

    def advance(self, n: int = 1) -> None:
        with _lock:
            self.done += n

    def items[T](self, items: Iterable[T], name: Callable[[T], str]) -> Iterator[T]:
        """Name the file being processed; count it after its iteration completes.

        A skipped file still advances. An exception leaves cleanup to job(),
        without claiming the failed operation finished.
        """
        for item in items:
            with _lock:
                self.item = name(item)
            yield item
            with _lock:
                self.done += 1
                self.item = None


_lock = threading.Lock()
_running: list[Job] = []
_finished: dict[str, Any] | None = None
_seq = itertools.count(1)


#: The job this context is running inside, so a job started within another is
#: shown as its step (#815): an album's Tagging inside Applying updates. A
#: ContextVar, so a job on another thread is never mistaken for a step.
_enclosing: ContextVar[Job | None] = ContextVar("progress_enclosing", default=None)


@contextmanager
def job(label: str, total: int, *, foreground: bool = False) -> Iterator[Job]:
    """Show `label` with `done`/`total` in the status bar while the block runs."""
    global _finished
    current = Job(label, total, foreground=foreground, parent=_enclosing.get())
    token = _enclosing.set(current)
    with _lock:
        _running.append(current)
    try:
        yield current
    except BaseException:
        with _lock:
            _running.remove(current)
        raise
    finally:
        _enclosing.reset(token)
    with _lock:
        _running.remove(current)
        if current.closing is not None:
            _finished = {
                "seq": next(_seq),
                "label": current.closing_verb or label,
                "message": current.closing,
                "level": current.closing_level,
            }


def status() -> dict[str, Any]:
    """Running jobs, oldest first, and the last closing line with its sequence
    number, which is how the bar tells a new line from one it has shown.

    A job running inside another is reported as that job's `step` rather than
    alongside it, and lends it its foreground priority: a file write inside a
    bulk job is still a file write the bar must not hide."""
    with _lock:
        jobs = []
        for j in _running:
            if j.parent is not None:
                continue
            step = next((c for c in reversed(_running) if c.parent is j), None)
            jobs.append(
                {
                    "label": j.label,
                    "done": j.done,
                    "total": j.total,
                    **({"item": j.item} if j.item is not None else {}),
                    **({"foreground": True} if j.foreground or (step and step.foreground) else {}),
                    **(
                        {"step": {"label": step.label, "done": step.done, "total": step.total}}
                        if step is not None
                        else {}
                    ),
                }
            )
        return {
            "jobs": jobs,
            "finished": dict(_finished) if _finished is not None else None,
        }


def reset() -> None:
    """Forget every job, for tests."""
    global _finished
    with _lock:
        _running.clear()
        _finished = None
