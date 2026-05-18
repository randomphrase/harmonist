"""Per-process path → UUID registry for albums without a sidecar.

The sidecar JSON is the long-term source of truth for an album's id (via
`mb_release_id` when matched, `temp_uid` when not). But NEW albums — dirs
the scanner has seen but for which no sidecar has been written yet — have
no on-disk record to read a UUID from. The inbox UI still needs *some* id
to wire its Reconcile/Recover/Manual buttons.

This module owns that gap: per-process, keyed by absolute path, mints a
UUID once and returns the same UUID on subsequent lookups. When the first
sidecar gets written for a path, `sidecar.write()` consults the registry
so the persisted `temp_uid` matches what the UI has already shown — the
URL stays the same across the NEW → sidecar'd transition.

In-memory only. Server restart clears the registry; NEW albums get fresh
UUIDs on next scan, which is fine because nobody bookmarks inbox URLs. A
directory rename of a NEW album (before any sidecar exists) drops the old
mapping and mints a new UUID for the new path — acceptable since NEW is a
transient state, usually resolved by auto-reconcile within seconds.
"""
from __future__ import annotations

import uuid
from pathlib import Path


_uids: dict[Path, str] = {}


def get_or_mint(path: Path) -> str:
    """Return the UUID for this path, minting a new one if none is known."""
    if path not in _uids:
        _uids[path] = uuid.uuid4().hex
    return _uids[path]


def peek(path: Path) -> str | None:
    """Return the UUID for this path if one has been minted, else None.

    Used by sidecar.write() to preserve the registry UUID when persisting
    a sidecar for the first time, without minting a fresh one.
    """
    return _uids.get(path)


def clear() -> None:
    """Drop all registrations. Test helper."""
    _uids.clear()
