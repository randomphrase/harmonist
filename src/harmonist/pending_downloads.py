"""In-memory store of "potential downloads".

A potential download is a Bandcamp **purchase** that a link-only (library-adoption)
sync could NOT confidently match to an on-disk album — no exact item_id, no
exact/slug store_url match — and so was NOT auto-downloaded. Instead it's surfaced
for an explicit user decision: Download · Match to an existing album · Don't
download (see ``usability-refactor.md`` §Phase 5).

**Not persisted.** This is runner-held, module-level state: populated by the sync,
read by the inbox, cleared on restart, and re-derived on the next sync. There is
deliberately no ``pending-downloads.json`` — it would add a schema/migration
burden for something cheap to recompute, and the *decisions* (Match → sidecar +
ignores.txt; Don't download → ignores.txt) persist through mechanisms that already
exist. Keyed by ``item_id`` so the same purchase never appears twice.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class PendingPurchase:
    """A Bandcamp purchase awaiting a download decision (no on-disk album yet)."""

    item_id: int
    band: str
    title: str
    url: str
    fmt: str  # the format it would download as (alac/flac/…)


_lock = threading.Lock()
_pending: dict[int, PendingPurchase] = {}


def replace_all(items: list[PendingPurchase]) -> None:
    """Atomically swap the whole set (the sync rebuilds the residue, then swaps it
    in at the end — so the inbox shows the previous set until the new one is ready,
    never a mid-sync empty flash)."""
    with _lock:
        _pending.clear()
        for p in items:
            _pending[p.item_id] = p


def remove(item_id: int) -> None:
    """Drop one purchase once the user has decided on it."""
    with _lock:
        _pending.pop(item_id, None)


def get(item_id: int) -> PendingPurchase | None:
    with _lock:
        return _pending.get(item_id)


def all_pending() -> list[PendingPurchase]:
    """Current potential downloads, sorted for stable display."""
    with _lock:
        return sorted(_pending.values(), key=lambda p: (p.band.lower(), p.title.lower()))


def count() -> int:
    with _lock:
        return len(_pending)
