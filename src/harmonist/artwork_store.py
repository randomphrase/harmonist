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
fill the disk. So the store is bounded — but by a promise rather than only by a
number (#408).

**The promise is per album: the last `keep_per_album` artwork changes on any
album can be undone.** A global byte cap alone could not make that promise, and
broke it in the way that matters least visibly — a background pass backing up
five hundred albums overnight would evict the copy behind the Undo button your
album was still offering, on a schedule nobody can predict. Retention is
therefore computed from the same records the UI reads (`activity_store`'s
artwork backups, grouped per tagging), so what is protected is exactly what the
page offers to restore rather than a second opinion about it.

The byte cap remains as a **backstop**, not the policy: it only bites once every
album is already down to its protected set, and when it does it takes the oldest
change first and says so loudly. A restore is still best-effort — an old enough
change becomes unrevertable and the UI says so — but "old enough" now means
"you have changed this album's artwork five times since", which a user can
reason about, rather than "someone else's albums needed the room".
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
#: beside a music library. Configurable; see `config.ArtworkStoreConfig`.
DEFAULT_MAX_BYTES = 500 * 1024 * 1024

#: How many artwork changes an album keeps. Five is enough to cover a session of
#: experimenting on one album and still be reasoning a user can hold: "the last
#: five artwork changes to any album can be undone". Counted in TAGGINGS, not
#: images, so a compilation whose four per-track covers are replaced in one go
#: spends one of its five rather than four.
DEFAULT_KEEP_PER_ALBUM = 5

#: Set at startup, like `audit.set_library_root`. None means no store is
#: configured — every call becomes a no-op rather than an error, because a
#: failure to keep a backup must never stop the tagging it was backing up.
_root: Path | None = None
_max_bytes: int = DEFAULT_MAX_BYTES
_keep_per_album: int = DEFAULT_KEEP_PER_ALBUM


def configure(
    root: Path | None,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    keep_per_album: int = DEFAULT_KEEP_PER_ALBUM,
) -> None:
    """Point the store at `root` (created on demand), with its retention."""
    global _root, _max_bytes, _keep_per_album
    _root = root
    _max_bytes = max_bytes
    _keep_per_album = keep_per_album


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


def protected_digests() -> frozenset[str]:
    """The images the retention promise covers: every digest replaced by each
    album's `keep_per_album` most recent artwork changes.

    Read from `activity_store`'s own records rather than from anything this
    module keeps, so the set is exactly what the album page offers an Undo for —
    `_restorable_anchors` walks the same taggings. A separate ledger here would
    be free to disagree with the page, and the symptom would be a button that
    vanishes or one that fails.

    Grouped per album under the id the tagging was RECORDED with, which is what
    makes a re-identified album keep its older backups: those rows carry its old
    id, and both sets are protected on their own terms.

    Empty when the store is unreachable — and that is the safe direction. An
    empty protected set makes eviction fall back to oldest-first over
    everything, which is the behaviour before #408; a spuriously *full* one
    would let the store grow past its cap on a broken read.
    """
    if _keep_per_album <= 0:
        return frozenset()
    # Imported here rather than at module scope: `audit` already reaches the
    # store, and a top-level import would make the artwork store depend on the
    # event store to be *loaded* — which it does not need in order to keep a file.
    from . import activity_store

    seen: dict[str, int] = {}
    out: set[str] = set()
    # Newest first, so an album's allowance is spent on its most recent changes.
    for backup in activity_store.artwork_backups():
        count = seen.get(backup.album_id, 0)
        if count >= _keep_per_album:
            continue
        seen[backup.album_id] = count + 1
        out |= backup.digests
    return frozenset(out)


def _evict_if_over_cap() -> None:
    """Drop images until the store is under cap, protected ones last.

    Two passes, and the order is the whole point (#408). The first spends the
    overage on images NO album's retention promise covers — a change already
    superseded five times over, which nothing on any page offers to undo. Only
    if that is not enough does the second pass touch protected images, oldest
    first, and it says so at WARNING: at that point the store is breaking a
    promise the UI has been making, and on an unattended box the log is the only
    place that can say so.

    Within each pass, least recently *referenced* rather than stored: `keep`
    touches an image it already holds, so an image shared by several albums is
    as fresh as its newest use.
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

    protected = protected_digests()
    by_age = sorted(entries, key=lambda e: e[1].st_mtime)
    passes = (
        [e for e in by_age if e[0].stem not in protected],
        [e for e in by_age if e[0].stem in protected],
    )
    for guarded, group in zip((False, True), passes, strict=True):
        for path, st in group:
            if total <= _max_bytes:
                return
            try:
                path.unlink()
            except OSError:
                continue
            total -= st.st_size
            if guarded:
                log.warning(
                    "artwork store is over its cap with nothing spare: dropped %s, "
                    "which an album could still have undone",
                    path.stem[:12],
                )
            # Audited: this is Harmonist deleting the only remaining copy of one
            # of the user's images, which is exactly what the audit log is for —
            # even though it is deleting it by a policy the user set.
            audit.record(
                "artwork.evict", digest=path.stem[:12], bytes=st.st_size, protected=guarded
            )
