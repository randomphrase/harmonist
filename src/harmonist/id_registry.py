"""Stable ids for albums that have no sidecar to read one from.

The sidecar JSON is the long-term source of truth for an album's id (via
`mb_release_id` when matched, `temp_uid` when not). But albums the scanner has
seen and for which no sidecar has been written yet have no on-disk record to
read a UUID from, and the UI still needs *some* id to wire its
Reconcile/Recover/Manual buttons to.

This module owns that gap. The id is a **hash of the album's path relative to
the library root** — deterministic, so the same directory always yields the same
id, on this run and every future one.

It used to be a random UUID held in a per-process dict, which was fine while the
id only had to survive one session ("nobody bookmarks inbox URLs"). It stopped
being fine once history became per-album: everything recorded against a
sidecar-less album was orphaned by the next restart, because the album came back
with a different id. That is not an edge case — `reconcile` writes no sidecar at
all for an album with no MBID and no recoverable store URL, so such an album
stays sidecar-less indefinitely (#114).

Two consequences worth knowing:

* **Relative to the library root**, not absolute, so re-pointing a bind-mount at
  the same library doesn't re-identify every album in it. `set_library_root()`
  is called once at startup, mirroring `audit.set_library_root()`.
* **A rename re-identifies** a sidecar-less album, since the path is the input.
  Unavoidable without a durable marker on disk, and mild: an album with a
  sidecar carries its id inside the file, which moves with the directory.

Hashed rather than used raw because the id goes in URL path segments, and most
routes carry a further segment after it (`/library/{album_id}/compare`) — a value
containing slashes can't sit there, and `%2F` is routinely mangled by reverse
proxies.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# Set once at startup. Until then ids derive from the absolute path, which keeps
# unit tests and non-web callers working — they just aren't portable across a
# different mount point, which no test cares about.
_library_root: Path | None = None

# Long enough that a collision across a library is not a practical concern,
# short enough to stay readable in a URL.
_ID_LENGTH = 32


def set_library_root(root: Path | None) -> None:
    """Point id derivation at the configured music dir (once, at startup)."""
    global _library_root
    _library_root = root


def _key(path: Path) -> str:
    """The string an album's id is derived from: its path relative to the library
    root, or the absolute path when it lies outside (or no root is set yet)."""
    if _library_root is not None:
        try:
            return str(path.relative_to(_library_root))
        except ValueError:
            pass
    return str(path)


def get_or_mint(path: Path) -> str:
    """This album directory's id. Deterministic — the name is kept for the
    call sites, but nothing is minted or stored any more."""
    return hashlib.sha256(_key(path).encode("utf-8")).hexdigest()[:_ID_LENGTH]


def peek(path: Path) -> str:
    """Same as `get_or_mint`; retained because `sidecar.write()` reads it to
    persist an album's existing id as its `temp_uid`, so the id doesn't change
    when the first sidecar appears.

    Returns `str`, not `str | None`. It never could return None — the id is a
    hash of the path — but the optional signature outlived the dict-backed
    version that could miss, and left `sidecar` carrying a fallback that was
    unreachable the moment ids became deterministic.
    """
    return get_or_mint(path)


def path_for(uid: str, candidates: list[Path]) -> Path | None:
    """Reverse lookup: which of `candidates` has this id.

    A hash can't be inverted, so the caller supplies the paths to check — in
    practice the current scan's albums, which is the same linear pass the old
    dict-based lookup did. Used by `_find_album` when a rendered URL holds the
    pre-sidecar id of an album whose canonical id has since changed.
    """
    return next((p for p in candidates if get_or_mint(p) == uid), None)
