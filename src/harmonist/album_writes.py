"""The folders a background write holds while it runs (#1053).

A confirmed review is written after its request has answered, so nothing on the
page stops the album being confirmed again while the first write is still
rewriting its files, and two writers over the same files would each rewrite them.
A write holds every folder it writes, the folders a review added included, until
it ends. A second write for any of them is refused rather than queued: the user
who asked for it is there to see why, and can ask again once the first is done.

The folders are held until the write has entirely finished: the files written,
the album read again and the outcome recorded. A page asking whether the write
is over is then never told so while the album is still at its old address.
`any_writing` is for the demo's reset, which replaces the whole library.

`ended` counts the writes that have finished, for the status poll, which redraws
the pages once it moves. The refresh after a write can publish a scan while the
folders are still held, so a page redrawn for that may show the album covered
for a moment; and a write that changes nothing a scan compares, such as one
that failed before writing or only moved a track assignment, publishes none.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from pathlib import Path

from .models import Album

_lock = threading.Lock()
_held: set[Path] = set()
_writes = 0
_ended = 0


def hold(folders: Iterable[Path]) -> bool:
    """Take `folders` for one write; False, taking none, while any is held.
    A write that holds them is counted until `finished`."""
    global _writes
    wanted = set(folders)
    with _lock:
        if wanted & _held:
            return False
        _held.update(wanted)
        _writes += 1
        return True


def release(folders: Iterable[Path]) -> None:
    """Give `folders` back, once the write has entirely finished, or failed to
    start."""
    with _lock:
        _held.difference_update(folders)


def finished() -> None:
    """Once per write `hold` admitted, when nothing more is left for it to do."""
    global _writes, _ended
    with _lock:
        _writes -= 1
        _ended += 1


def ended() -> int:
    """How many writes have finished since Harmonist started."""
    with _lock:
        return _ended


def writing(album: Album) -> bool:
    """Whether a background write holds any of `album`'s folders."""
    with _lock:
        return any(folder in _held for folder in album.folders)


def any_writing() -> bool:
    """Whether any background write has yet to finish."""
    with _lock:
        return _writes > 0
