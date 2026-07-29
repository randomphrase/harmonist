"""Audit log of potentially-destructive operations.

A dedicated ``harmonist.audit`` logger records — in detail and greppably — every
action that writes, moves, overwrites, or deletes user data: downloads (with
target path + format), file moves/overwrites, sidecar identity rewrites, state
demotions/surrenders, checkpoint clears, case-collisions. The point is
transparency (the project's guiding principle): when something unexpected
happens to the library, there is a precise, timestamped record of exactly what
Harmonist did and when.

Audit lines go to the server log (INFO, under one logger name so they're trivial to
filter — ``grep harmonist.audit``) AND to the durable `activity_store` (issue #33)
tagged ``source="audit"``, so the record of what Harmonist did to user data survives
a restart and log rotation. It's the same one-line ``event key=value …`` string in
both places. Distinct from the ``activity`` feed, which stores the user-facing
outcomes (``source="activity"``) and does not show audit rows.
"""

from __future__ import annotations

import logging

from . import activity_store
from .activity_store import Level, Source

log = logging.getLogger("harmonist.audit")


def record(event: str, *, album_id: str | None = None, **fields: object) -> None:
    """Record one audit event as ``event key=value …`` — to the server log and to
    the durable store.

    ``album_id`` (an album's ``Album.id``) is a structured column, not part of the
    message: it ties the row to an album so per-album history spans activity+audit
    (#33). Values containing whitespace (album paths!) are quoted so each event
    stays a single, parseable line. None is rendered as ``-``.
    """
    line = event if not fields else f"{event} {_detail(fields)}"
    log.info("%s", line)
    activity_store.append(message=line, level=Level.INFO, source=Source.AUDIT, album_id=album_id)


def _detail(fields: dict[str, object]) -> str:
    return " ".join(f"{key}={_fmt(value)}" for key, value in fields.items())


def _fmt(value: object) -> str:
    if value is None:
        return "-"
    s = str(value)
    return f'"{s}"' if (not s or any(c.isspace() for c in s)) else s
