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


@dataclass(frozen=True)
class CarriedMatch:
    """What an archived album knew about itself, for its replacement to inherit."""

    mb_release_id: str
    #: The archived copy was INCOMPLETE — short of the release's tracklist, and
    #: tagged that way regardless. Carried because it is part of the match the
    #: user accepted, not a separate judgement: a replacement that arrives just
    #: as short is the status quo, and refusing to tag it would turn a tagged
    #: album into inbox work for a shortfall it already had. The reverse case is
    #: the one that must NOT be waved through — an album that was complete coming
    #: back short is a bad download, so it does not get this.
    incomplete: bool = False


# item_id → what the archived album was, for the replacement to be tagged as too
# (#132).
#
# **Deliberately not the same lifetime as the card above**, which is why it is a
# second dict rather than a field on `PendingRedownload`. The card clears the
# moment the files are back on disk; this must survive until the *tagging*, which
# happens afterwards — and the inbox polls every couple of seconds during a sync,
# so the gap between the two is not theoretical. Sharing one dict meant a poll
# landing in that window silently threw the release away and the replacement
# re-resolved from scratch: the exact thing carrying it is meant to prevent.
#
# Consumed by `take_release`, so an entry has a definite end. One that is never
# consumed — the download never arrives — leaks a single string until restart,
# which is the same bound as everything else in this module.
_carried: dict[int, CarriedMatch] = {}


def add(entry: PendingRedownload, *, match: CarriedMatch | None = None) -> None:
    """Record an archived album as awaiting re-download.

    `match` is what the archived copy was. Re-downloading says the *files* are
    wrong, not the match — so the replacement is tagged as that same release
    rather than being re-resolved from its store URL, which could land on a
    different one (or on none).
    """
    with _lock:
        _pending[entry.item_id] = entry
        if match:
            _carried[entry.item_id] = match


def take_match(item_id: int) -> CarriedMatch | None:
    """What a re-download should be tagged as, removing it as it answers.

    Take-once: the tagging either succeeds, or falls back to resolving the store
    URL and must not then be re-attempted against the same release on a later
    sync — by which point the user has seen the album in their inbox and may have
    assigned it something else themselves.
    """
    with _lock:
        return _carried.pop(item_id, None)


def remove(item_id: int) -> None:
    with _lock:
        _pending.pop(item_id, None)
        _carried.pop(item_id, None)


def prune(present_item_ids: set[int]) -> None:
    """Drop every entry whose purchase is back on disk.

    `present_item_ids` is `library_index.item_ids()` — the purchases the current
    scan can see. An entry in both is an album that has returned.

    Clears the CARD only. The carried release outlives it on purpose: the album
    is on disk here but not yet tagged, and that is precisely when the release is
    still needed (see `_carried`).
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
        _carried.clear()
