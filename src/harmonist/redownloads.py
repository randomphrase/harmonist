"""In-memory record of albums archived and waiting to come back (#132).

Between the archive and the sync that re-fetches it, a re-downloaded album is
nowhere: its directory is gone, so the scanner cannot see it and it is in no
state, no count and no filter. Without this it would simply vanish from the UI
for the length of a sync, which is not an acceptable thing to do to someone who
just clicked a button that deleted their files.

**Not persisted**, for the same reasons as `pending_downloads` — a
`redownloads.json` buys a schema and a migration for something re-derivable, and
the *decision* already persists through mechanisms that exist: the item_id is out
of `ignores.txt` and the directory is off disk, which is exactly what makes the
next sync fetch it, restart or no restart.

What a restart does lose is the in-memory download approval alongside this card.
On a library with unlinked albums the next sync then runs link-only, and the
purchase surfaces as an ordinary *potential download* instead — one click to
fetch, but sitting next to a "Don't download" that would strand it. The window is
the few seconds between the archive and the sync it kicks, and the recovery
(Restore in Settings, then unzip) exists, so this is accepted rather than
engineered around.

**Cleared by derivation, not by lifecycle.** Nothing here is told when a download
lands; `prune` is handed the item_ids the library currently holds and drops
whatever has come back. So a re-download that arrives by any route — the sync we
kicked, a later one, the user restoring the zip by hand — clears the card, and
there is no completion callback to forget to call.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class PendingRedownload:
    """An album archived and awaiting its replacement download."""

    item_id: int
    artist: str
    title: str
    #: Bandcamp URL of the purchase, so the card can link out to it.
    url: str
    #: Filename of the archive holding the old files, so the card can say where
    #: they went without the user hunting for it.
    archive_name: str
    requested_at: datetime

    @property
    def label(self) -> str:
        return f"{self.artist} — {self.title}".strip(" —")


_lock = threading.Lock()
_pending: dict[int, PendingRedownload] = {}


def add(entry: PendingRedownload) -> None:
    """Record an archived album as awaiting re-download."""
    with _lock:
        _pending[entry.item_id] = entry


def remove(item_id: int) -> None:
    with _lock:
        _pending.pop(item_id, None)


def prune(present_item_ids: set[int]) -> None:
    """Drop every entry whose purchase is back on disk.

    `present_item_ids` is `library_index.item_ids()` — the purchases the current
    scan can see. An entry in both is an album that has returned.
    """
    with _lock:
        for item_id in present_item_ids & set(_pending):
            del _pending[item_id]


def all_pending() -> list[PendingRedownload]:
    """Awaited re-downloads, oldest request first — the order they were asked for
    is the order they will arrive in."""
    with _lock:
        return sorted(_pending.values(), key=lambda r: r.requested_at)


def count() -> int:
    with _lock:
        return len(_pending)


def reset() -> None:
    """Clear all state. For demo re-seed and test isolation."""
    with _lock:
        _pending.clear()
