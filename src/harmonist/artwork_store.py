"""Somewhere to put the artwork a tagging is about to destroy (#131).

Embedding a cover overwrites whatever image the track already carried, and
until now that image was simply gone. This keeps it, so the change can be
undone.

**Content-addressed files, not database rows.** Cover art runs 200 KB–5 MB per
track and is *mostly identical between tracks of the same album* — the natural
shape for it is dedup by digest, not one row per file. An eight-track album
therefore usually costs one file, not eight. Keeping the bytes out of
`activity.db` also keeps that store small and cheap to poll: it is read on every
feed refresh, and inflating its pages with images the feed never selects would
make every query walk past them.

The audit record already holds the digests — `tagger` records `artwork` as
sha256 before/after on any tagging that replaces art (#86) — so nothing here
needs its own index. The digest in the record IS the lookup key.

**Bounded, and honest about it.** Nothing prunes `activity.db` today, which is
fine for text and is not fine for images: unattended re-tagging on a NAS would
fill the disk. So the store has a size cap and evicts oldest-first, and a
restore is therefore best-effort — an old enough change becomes unrevertable and
the UI has to say so. That is the deliberate trade against unbounded growth,
made where the user can see it (a Settings figure) rather than discovered when a
volume fills up.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

from . import audit

log = logging.getLogger(__name__)

#: Default cap. Around a thousand typical covers — enough that undoing a
#: re-tagging session weeks later still works, small enough to be unremarkable
#: beside a music library. Configurable; see `config.ArtworkConfig`.
DEFAULT_MAX_BYTES = 500 * 1024 * 1024

#: Set at startup, like `audit.set_library_root`. None means no store is
#: configured — every call becomes a no-op rather than an error, because a
#: failure to keep a backup must never stop the tagging it was backing up.
_root: Path | None = None
_max_bytes: int = DEFAULT_MAX_BYTES


def configure(root: Path | None, *, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
    """Point the store at `root` (created on demand), with a size cap."""
    global _root, _max_bytes
    _root = root
    _max_bytes = max_bytes


def digest(data: bytes) -> str:
    """The content address of `data`. sha256, matching what the tagging audit
    records — the two must agree or a stored image can never be found again."""
    return hashlib.sha256(data).hexdigest()


def keep(data: bytes, *, mime: str | None = None) -> str | None:
    """Store `data` under its digest and return that digest, or None if the
    store isn't configured or the write failed.

    Idempotent: an image already held is not rewritten, which is what makes an
    album whose tracks share one cover cost one file rather than one per track.

    Best-effort by design. A backup that cannot be written is a reason to warn,
    not a reason to abandon the tagging — the user asked for the re-tag, and
    refusing it because the undo store is full would be a worse failure than
    losing the undo. Returns None so the caller can record honestly that no
    copy was kept.
    """
    root = _root
    if root is None:
        return None
    key = digest(data)
    path = _path_for(root, key, mime)
    try:
        if path.exists():
            # Already held — but mark it as referenced NOW. Eviction is
            # oldest-first, and without this the mtime stays at first-store
            # time: two albums sharing an image (a label's house sleeve, a
            # reissue) would let a change made today be evicted before changes
            # made months ago, because the FILE is old even though the change
            # is not. What must survive is the most recently referenced image.
            os.utime(path)
            return key
        root.mkdir(parents=True, exist_ok=True)
        # Written via a temp file in the same directory then renamed, so a
        # crash can't leave a half-image under a digest that claims to be
        # complete — the same atomicity the sidecar writes use.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError:
        log.exception("could not keep artwork %s — the change will not be undoable", key[:12])
        return None
    audit.record("artwork.keep", digest=key, bytes=len(data))
    _evict_if_over_cap()
    return key


def path_for(key: str) -> Path | None:
    """Where `key`'s image lives, or None if the store no longer has it.

    None is an ordinary answer, not an error: the cap evicts oldest-first, so an
    old enough image is genuinely gone and the caller must offer no undo rather
    than a button that fails.
    """
    root = _root
    if root is None or not _is_digest(key):
        return None
    for candidate in root.glob(f"{key}.*"):
        if not candidate.name.endswith(".tmp"):
            return candidate
    return None


def usage() -> tuple[int, int]:
    """`(bytes_used, cap)` — what Settings shows."""
    root = _root
    if root is None or not root.is_dir():
        return 0, _max_bytes
    total = 0
    for entry in root.iterdir():
        try:
            total += entry.stat().st_size
        except OSError:
            continue
    return total, _max_bytes


def _path_for(root: Path, key: str, mime: str | None) -> Path:
    return root / f"{key}{_suffix(mime)}"


def _suffix(mime: str | None) -> str:
    """A real extension, so the files are openable by anything that finds them.

    The user may well go looking in this directory, and `<64 hex chars>` with no
    extension is hostile to every image viewer.
    """
    if mime and "png" in mime.lower():
        return ".png"
    return ".jpg"


def _is_digest(key: str) -> bool:
    """Guard the glob: `key` reaches here from a stored record, and a value
    containing a path separator or a wildcard must never be joined to a path."""
    return len(key) == 64 and all(c in "0123456789abcdef" for c in key)


def _evict_if_over_cap() -> None:
    """Drop the least recently referenced images until the store is under cap.

    Least recently *referenced*, not stored: `keep` touches an image it already
    holds, so an image shared by several albums is as fresh as its newest use.
    The oldest changes are the least likely to be undone, which is what makes
    this the right thing to lose first.
    """
    root = _root
    if root is None or not root.is_dir():
        return
    try:
        entries = [(p, p.stat()) for p in root.iterdir() if p.is_file()]
    except OSError:
        log.exception("could not read the artwork store to enforce its size cap")
        return
    total = sum(st.st_size for _, st in entries)
    if total <= _max_bytes:
        return
    for path, st in sorted(entries, key=lambda e: e[1].st_mtime):
        if total <= _max_bytes:
            break
        try:
            path.unlink()
        except OSError:
            continue
        total -= st.st_size
        # Audited: this is Harmonist deleting the only remaining copy of one of
        # the user's images, which is exactly what the audit log is for — even
        # though it is deleting it by a policy the user set.
        audit.record("artwork.evict", digest=path.stem[:12], bytes=st.st_size)
