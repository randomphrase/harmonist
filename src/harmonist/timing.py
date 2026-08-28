"""Saying when an operation took too long (#300).

Harmonist has had no way to report slowness. Every operation either succeeded
silently or failed loudly, so a page that took ninety seconds and a page that
never returned produced exactly the same log: nothing at all.

That is not a cosmetic gap. #299 is an album page that stalls after a restart on
a NAS the developer is not sitting in front of, and it has two entirely
different explanations — a background pass competing for the CPU, or a
rate-limited MusicBrainz fetch — with no way to tell them apart after the fact.
The log is the only channel for anything that happens while nobody is watching,
and "slow" is exactly the kind of thing it was silent about.

So: a guard that says nothing when things are fine, and one WARNING line when
they are not.

**WARNING, not ERROR.** Nothing was lost and nothing stopped working; this is
the "recovered / degraded" level. **Silent under the threshold**, so a healthy
install pays one `time.monotonic()` and adds no noise.

Deliberately standalone. It does NOT import `audit`, whose `key=value`
formatting this mirrors, because `audit` pulls in the SQLite activity store and
a logging helper has no business dragging a database behind it. The convention
is shared; the code is six lines and is not.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

log = logging.getLogger(__name__)


@contextmanager
def warn_if_slow(what: str, threshold: timedelta, **context: object) -> Iterator[None]:
    """Log one WARNING if the block takes `threshold` or longer. Otherwise silent.

    `context` becomes `key=value` pairs on the line, matching the audit log's
    convention, so the warning names *which* album or release was slow rather
    than reporting that something, somewhere, was. A line nobody can act on is
    barely better than no line.

    **Wrap a whole pass, never one iteration of a loop.** Twenty-seven warnings
    for one slow album is the noise the error-handling rules warn about; one
    warning naming the album is the signal. Where the worst item matters, the
    caller should find it and pass it as context.

    Reports a slow block that **raised**, too. The exception is somebody else's
    to log, but how long it took before failing is a fact worth having — a
    MusicBrainz call that fails after thirty seconds and one that fails at once
    are different problems.

    Thresholds are constants at their call sites, not settings. They are the
    period of a backstop nobody should have to reason about, and a user has
    nothing to tune them against (the same argument `_RESCAN_INTERVAL` makes).
    """
    started = time.monotonic()
    try:
        yield
    finally:
        elapsed = time.monotonic() - started
        if elapsed >= threshold.total_seconds():
            log.warning(
                "%s took %.1fs (over %.1fs)%s",
                what,
                elapsed,
                threshold.total_seconds(),
                f" {_detail(context)}" if context else "",
                # Keep it out of the user-facing Activity feed. `activity`
                # mirrors every WARNING from a `harmonist.*` logger into the
                # feed so background failures are visible — a good rule that
                # assumed every warning is news. This one is a measurement:
                # nothing went wrong and there is nothing to act on. Without
                # the flag, the conditions most worth investigating are exactly
                # the ones that would bury the feed in rows about themselves.
                extra={"_diagnostic": True},
            )


def _detail(fields: dict[str, object]) -> str:
    return " ".join(f"{key}={_fmt(value)}" for key, value in fields.items())


def _fmt(value: object) -> str:
    """One context value, quoted when it contains whitespace — album paths do,
    and an unquoted one would split the line into unparseable fragments."""
    if value is None:
        return "-"
    s = str(value)
    return f'"{s}"' if (not s or any(c.isspace() for c in s)) else s
