"""Audit log of potentially-destructive operations.

A dedicated ``harmonist.audit`` logger records — in detail and greppably — every
action that writes, moves, overwrites, or deletes user data: downloads (with
target path + format), file moves/overwrites, sidecar identity rewrites, state
demotions/surrenders, checkpoint clears, case-collisions. The point is
transparency (the project's guiding principle): when something unexpected
happens to the library, there is a precise, timestamped record of exactly what
Harmonist did and when.

Separate from ``activity`` (the in-memory, user-facing feed). Audit lines go to
the server log only, at INFO, under one logger name so they're trivial to filter
(``grep harmonist.audit``).
"""

from __future__ import annotations

import logging

log = logging.getLogger("harmonist.audit")


def record(event: str, **fields: object) -> None:
    """Log one audit event as ``event key=value …``.

    Values containing whitespace (album paths!) are quoted so each event stays a
    single, parseable line. None is rendered as ``-``.
    """
    if not fields:
        log.info("%s", event)
        return
    detail = " ".join(f"{key}={_fmt(value)}" for key, value in fields.items())
    log.info("%s %s", event, detail)


def _fmt(value: object) -> str:
    if value is None:
        return "-"
    s = str(value)
    return f'"{s}"' if (not s or any(c.isspace() for c in s)) else s
