"""Authoritative in-memory index of the library's sidecars + the dedup indexes
derived from them: ``item_id``, exact ``store_url``, and release ``slug``.

This is the single source for sync-time dedup / linking WITHOUT re-reading sidecars
from disk — the library scan already read every ``.harmonist.json``, so a paged
sync should never sweep the disk again per purchase.

**Kept current in ONE place, not sprinkled through the codebase:**
  - ``reset_from(albums)`` — authoritative full rebuild after each scan
    (self-healing: any drift from a missed update is corrected here);
  - ``upsert`` / ``remove`` — hooked into the sidecar **write / delete choke
    point**, so every link, demote, tag, download and reconcile keeps the indexes
    current with no per-call-site bookkeeping.

Module-global (same pattern as ``live_counts``), guarded by a lock because the
sync runs on a background thread.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from pathlib import Path

from .models import Album, Sidecar
from .url_recovery import album_slug

_lock = threading.Lock()
_sidecars: dict[Path, Sidecar] = {}
_by_item_id: dict[int, Path] = {}
_by_url: dict[str, Path] = {}
_by_slug: dict[str, set[Path]] = {}


def _index(album_dir: Path, sc: Sidecar) -> None:
    """Add sc's index entries. Caller holds the lock and has stored the sidecar."""
    if sc.bandcamp is not None and sc.bandcamp.item_id is not None:
        _by_item_id[int(sc.bandcamp.item_id)] = album_dir
    if sc.store_url:
        _by_url[sc.store_url] = album_dir
        if slug := album_slug(sc.store_url):
            _by_slug.setdefault(slug, set()).add(album_dir)


def _deindex(album_dir: Path, sc: Sidecar) -> None:
    """Remove sc's index entries. Caller holds the lock."""
    if (
        sc.bandcamp is not None
        and sc.bandcamp.item_id is not None
        and _by_item_id.get(int(sc.bandcamp.item_id)) == album_dir
    ):
        _by_item_id.pop(int(sc.bandcamp.item_id), None)
    if sc.store_url:
        if _by_url.get(sc.store_url) == album_dir:
            _by_url.pop(sc.store_url, None)
        if (slug := album_slug(sc.store_url)) and slug in _by_slug:
            _by_slug[slug].discard(album_dir)
            if not _by_slug[slug]:
                del _by_slug[slug]


def _clear_locked() -> None:
    _sidecars.clear()
    _by_item_id.clear()
    _by_url.clear()
    _by_slug.clear()


# ----- the single update points -----


def reset_from(albums: Iterable[Album]) -> None:
    """Rebuild the whole index from a fresh scan — authoritative + self-healing."""
    with _lock:
        _clear_locked()
        for a in albums:
            if a.sidecar is not None:
                _sidecars[a.path] = a.sidecar
                _index(a.path, a.sidecar)


def upsert(album_dir: Path, sc: Sidecar) -> None:
    """Index a written sidecar (hooked at ``sidecar.write`` — the one write point)."""
    with _lock:
        old = _sidecars.get(album_dir)
        if old is not None:
            _deindex(album_dir, old)
        _sidecars[album_dir] = sc
        _index(album_dir, sc)


def remove(album_dir: Path) -> None:
    """Drop a deleted sidecar (hooked at the sidecar delete point)."""
    with _lock:
        old = _sidecars.pop(album_dir, None)
        if old is not None:
            _deindex(album_dir, old)


def clear() -> None:
    """Drop everything (e.g. erase-sidecars before the rescan refills it)."""
    with _lock:
        _clear_locked()


# ----- lookups (zero disk) -----


def item_ids() -> set[int]:
    """Every linked purchase id on disk — the dedup seed."""
    with _lock:
        return set(_by_item_id)


def dir_for_url(url: str) -> Path | None:
    """Album dir whose store_url is exactly `url`, or None."""
    with _lock:
        return _by_url.get(url)


def slug_copies(url: str) -> list[tuple[Path, bool]]:
    """Every on-disk album sharing `url`'s release slug, as ``(album_dir, linked)``
    — subdomain-insensitive, inclusive of linked albums (the dedup backstop)."""
    slug = album_slug(url)
    if slug is None:
        return []
    with _lock:
        out: list[tuple[Path, bool]] = []
        for d in _by_slug.get(slug, set()):
            sc = _sidecars.get(d)
            linked = bool(sc and sc.bandcamp and sc.bandcamp.item_id is not None)
            out.append((d, linked))
        return out


def unlinked_slug_match(url: str) -> Path | None:
    """The single UNLINKED album sharing `url`'s slug, or None (0 or 2+ → can't
    pick) — the link target for a cross-listing whose id we don't yet have."""
    matches = [d for d, linked in slug_copies(url) if not linked]
    return matches[0] if len(matches) == 1 else None
