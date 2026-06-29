"""Process-wide live album-state counts — the single source of truth for the
inbox/library numbers the UI shows.

The authoritative source is the scanner: every completed scan calls
``reset_from`` with the fresh snapshot, so the counts are self-healing — any
drift from a missed or imperfect transition is corrected on the next scan.
Between scans, ``move`` applies a transition (decrement old state, increment
new) so the numbers update the instant an album changes — during a long sync or
reconcile — without re-walking the library.

Replaces the three competing count sources we'd grown (the scan snapshot's group
lengths, reconcile's live tallies, and JS marker badges). Thread-safe: scans
reset from the loop thread; transitions fire from the sync/reconcile/request
threads.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Iterable

from .models import Album, AlbumState

# Terminal = "in the Library, off the inbox" (see models.py / docs §3).
_TERMINAL = frozenset({AlbumState.COMPLETE, AlbumState.INCOMPLETE})

_lock = threading.Lock()
_counts: Counter[AlbumState] = Counter()


def reset_from(albums: Iterable[Album]) -> None:
    """Authoritatively set the counts from a fresh scan snapshot (self-healing)."""
    fresh: Counter[AlbumState] = Counter(a.state for a in albums)
    with _lock:
        global _counts
        _counts = fresh


def move(old: AlbumState | None, new: AlbumState | None) -> None:
    """Apply a transition: one album left ``old`` and entered ``new``. Either may
    be None (an album appearing / disappearing). No-op when old == new. Floors at
    zero so a double-applied transition can't push a count negative (the next
    scan's reset_from corrects any drift)."""
    if old is new:
        return
    with _lock:
        if old is not None and _counts[old] > 0:
            _counts[old] -= 1
        if new is not None:
            _counts[new] += 1


def to_status() -> dict[str, int]:
    """The UI-facing buckets: derived inbox/library totals plus per-state counts
    for the inbox group headers."""
    with _lock:
        c = dict(_counts)
    inbox = sum(n for s, n in c.items() if s not in _TERMINAL)
    library = sum(n for s, n in c.items() if s in _TERMINAL)
    return {
        "inbox": inbox,
        "library": library,
        "new": c.get(AlbumState.NEW, 0),
        "needs_mbid": c.get(AlbumState.NEEDS_MBID, 0),
        "needs_sync": c.get(AlbumState.NEEDS_SYNC, 0),
        "tagging": c.get(AlbumState.TAGGING, 0),
        "inconsistent": c.get(AlbumState.INCONSISTENT, 0),
        "complete": c.get(AlbumState.COMPLETE, 0),
        "incomplete": c.get(AlbumState.INCOMPLETE, 0),
    }
