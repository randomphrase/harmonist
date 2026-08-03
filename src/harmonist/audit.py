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
from pathlib import Path

from . import activity_store
from .activity_store import Level, Source

log = logging.getLogger("harmonist.audit")

# The library root, so recorded paths are relative to it (#98). An absolute path
# is mostly noise: under Docker the prefix is a container path (`/music`) that
# means nothing to the user, and on any install the same prefix repeats on every
# line while the part that identifies the album is the tail.
#
# Set once at startup and read HERE rather than threaded through call sites,
# because the writers — sidecar.py, cover_art.py, tagger.py, bandcamp_hook.py —
# have no idea where the library is and shouldn't need to. `_fmt` is the single
# point every audit value passes through.
_library_root: Path | None = None


def set_library_root(path: Path | None) -> None:
    """Record paths relative to `path`. None (the default) keeps them absolute,
    which is what tests and any pre-startup write get."""
    global _library_root
    _library_root = path


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
    s = _rel(value) if isinstance(value, Path) else str(value)
    return f'"{s}"' if (not s or any(c.isspace() for c in s)) else s


def _rel(p: Path) -> str:
    """`p` relative to the library root, else unchanged.

    Only `Path` values are rewritten — a string that happens to look like a path
    is left alone, since guessing would risk mangling ordinary field values.
    Anything outside the library (or the root itself) stays absolute: a relative
    path that escapes upward would be less readable, not more."""
    root = _library_root
    if root is None:
        return str(p)
    try:
        rel = p.relative_to(root)
    except ValueError:
        return str(p)  # outside the library — absolute is the honest rendering
    return str(rel) if rel.parts else str(p)
