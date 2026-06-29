"""Per-album reconciliation: derive a sidecar from existing tags + MB lookup.

For an album that's already tagged (has a `MusicBrainz Album Id` atom) but
has no `.harmonist.json` sidecar, reconcile_album decides what `store_url`
to record (if any):

  * If the file's `©cmt` tag contains any `bandcamp.com` URL **and** MB has
    a Bandcamp URL relationship for the release → `store_url` set to MB's
    canonical URL, `bandcamp.item_id=None`. The album shows as NEEDS_SYNC
    until the next sync fills in item_id by matching against the user's
    purchase list.

  * Otherwise → no `store_url`. Album shows as DONE.

If the album has **no** MBID atom (e.g. a Bandcamp download added by hand,
never run through Picard), we instead try to recover its Bandcamp store URL
from the `©cmt` comment. On success we write a sidecar with that `store_url`
and no MBID, so the album advances NEW → NEEDS_MBID (then, once tagged,
NEEDS_SYNC picks up its Bandcamp item_id). Without this an untagged download
would sit in NEW forever, or tag straight to COMPLETE and never sync.

Pure: no globals. Caller injects `fetch_urls` (MB lookup) and `recover_url`
(Bandcamp URL recovery) so tests don't need real network.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from . import formats, url_recovery
from . import sidecar as sidecar_mod
from .models import Sidecar
from .sidecar import CURRENT_SCHEMA_VERSION

log = logging.getLogger(__name__)


def reconcile_album(
    album_dir: Path,
    *,
    fetch_urls: Callable[[str], list[str]],
    recover_url: Callable[[Path], str | None] = url_recovery.recover_store_url,
) -> Sidecar | None:
    """Inspect the album, write a sidecar, return it. None if nothing to do.

    Two jobs:
      * No sidecar → derive one from the file tags (or recover a Bandcamp URL).
      * Sidecar present but its `mb_release_id` disagrees with a *consistent*
        file MBID → **adopt the file tags** (the user re-tagged in Picard, as we
        ask them to). Otherwise leave an existing sidecar untouched.
    """
    files = sorted(p for p in album_dir.iterdir() if formats.is_supported(p))
    if not files:
        return None

    existing: Sidecar | None = None
    if sidecar_mod.has_sidecar(album_dir):
        try:
            existing = sidecar_mod.read(album_dir)
        except Exception:
            return None  # unreadable sidecar — don't touch it
    if existing is not None:
        # Adopt an external re-tag: the files now carry a different consistent
        # MBID than the sidecar records (the TAGGING-state mismatch). Files win —
        # re-point the sidecar, keeping store_url / item_id (same purchase), and
        # clear the now-stale candidate + expected-track-count. Scoped to a
        # sidecar that HAS an MBID, so a surrendered album (no MBID, but files
        # still carry the old one) is NOT re-promoted.
        file_mbid = _consistent_file_mbid(files)
        if existing.mb_release_id and file_mbid and file_mbid != existing.mb_release_id:
            adopted = replace(
                existing,
                mb_release_id=file_mbid,
                mb_match_candidate=None,
                track_count_expected=None,
                tagged_at=datetime.now(UTC),
            )
            sidecar_mod.write(album_dir, adopted)
            return adopted
        return None  # sidecar present + consistent (or files untagged) → leave it

    mbid, comment = _read_album_id_and_comment(files)
    now = datetime.now(UTC)

    if not mbid:
        # No MBID atom — try to recover the Bandcamp store URL from the comment
        # so the album can advance NEW → NEEDS_MBID instead of stalling in NEW.
        return _reconcile_untagged(album_dir, recover_url, now)

    bandcamp_url = matching_bandcamp_url(mbid, comment, fetch_urls)
    sc = Sidecar(
        schema_version=CURRENT_SCHEMA_VERSION,
        store_url=bandcamp_url or None,
        mb_release_id=mbid,
        added_at=now,
        tagged_at=now,
    )
    sidecar_mod.write(album_dir, sc)
    return sc


def _reconcile_untagged(
    album_dir: Path, recover_url: Callable[[Path], str | None], now: datetime
) -> Sidecar | None:
    """For an album with no MBID atom: recover its Bandcamp store URL (if any)
    and record it. Returns the sidecar (NEEDS_MBID — no MBID, no tagged_at), or
    None when no URL is recoverable (album stays an Orphan)."""
    try:
        recovered = recover_url(album_dir)
    except Exception as e:
        log.warning("URL recovery failed for %s: %s", album_dir, e)
        return None
    if not recovered:
        return None
    sc = Sidecar(
        schema_version=CURRENT_SCHEMA_VERSION,
        store_url=recovered,
        mb_release_id=None,  # untagged — lands in NEEDS_MBID, not NEEDS_SYNC
        added_at=now,
    )
    sidecar_mod.write(album_dir, sc)
    return sc


def reconcile_pending(
    album_dirs: list[Path],
    *,
    fetch_urls: Callable[[str], list[str]],
    recover_url: Callable[[Path], str | None] = url_recovery.recover_store_url,
) -> dict[str, int]:
    """Reconcile a batch of album dirs. Returns a stats summary."""
    stats = {"reconciled_bandcamp": 0, "reconciled_manual": 0, "skipped": 0, "errors": 0}
    for d in album_dirs:
        try:
            sc = reconcile_album(d, fetch_urls=fetch_urls, recover_url=recover_url)
        except Exception as e:
            log.warning("reconcile failed for %s: %s", d, e)
            stats["errors"] += 1
            continue
        if sc is None:
            stats["skipped"] += 1
        elif sc.store_url:
            stats["reconciled_bandcamp"] += 1
        else:
            stats["reconciled_manual"] += 1
    return stats


def _consistent_file_mbid(files: list[Path]) -> str | None:
    """The single MB Album Id shared by all tagged files, or None if no file is
    tagged or they disagree (we don't pick a winner among inconsistent tags —
    those are surfaced as INCONSISTENT for the user to split into folders)."""
    ids = {mid for f in files if (mid := formats.read_album_id(f))}
    return next(iter(ids)) if len(ids) == 1 else None


def _read_album_id_and_comment(files: list[Path]) -> tuple[str | None, str]:
    """Return (mbid, comment) from the first file that has an MBID atom."""
    for f in files:
        mbid = formats.read_album_id(f)
        if mbid:
            return mbid, formats.read_comment(f) or ""
    return None, ""


def store_url_for_tagging(
    album_dir: Path,
    mbid: str,
    *,
    fetch_urls: Callable[[str], list[str]],
) -> str | None:
    """The best deterministic Bandcamp store URL for an album being tagged to
    `mbid`, or None — used at tag time so a manually-assigned download reaches
    Needs Sync (not Complete) when it's a Bandcamp purchase.

    No guessing, three sources in preference order (precise first):
      1. The fully-formed `/album/` (or `/track/`) URL embedded in the file's
         `©cmt` — the actual purchase URL.
      2. MB's canonical Bandcamp URL for the release (a precise `/album/` URL).
      3. Last resort: the artist-root Bandcamp URL from the `©cmt` (e.g.
         `artist.bandcamp.com`) as a placeholder — enough to mark the album a
         Bandcamp purchase; the sync then links it to a purchase by title.

    Everything is gated by Bandcamp evidence in the `©cmt`: with no Bandcamp URL
    in the comment at all, returns None (a CD rip stays Complete, not Needs Sync).
    """
    files = sorted(p for p in album_dir.iterdir() if formats.is_supported(p))
    if not files:
        return None
    comment = formats.read_comment(files[0]) or ""
    url = url_recovery.extract_bandcamp_url(comment)
    if url is None:
        return None  # no Bandcamp evidence → not a Bandcamp purchase
    # 1. A precise release URL embedded in the comment is the real purchase URL.
    if "/album/" in url or "/track/" in url:
        return url
    # 2. Otherwise prefer MB's canonical /album/ URL when it has one.
    if mb_url := matching_bandcamp_url(mbid, comment, fetch_urls):
        return mb_url
    # 3. Fall back to the artist-root URL as a placeholder (→ title-linked on sync).
    return url


def matching_bandcamp_url(
    mbid: str,
    comment: str,
    fetch_urls: Callable[[str], list[str]],
) -> str | None:
    """Return MB's canonical Bandcamp URL for the release, or None.

    Requires BOTH:
      - The file's ©cmt tag mentions a bandcamp.com URL (evidence of purchase).
      - MB has at least one Bandcamp URL relationship for the release.

    Used as the fallback when no fully-formed Bandcamp URL is embedded in the
    comment; the recorded URL is MB's canonical one.
    """
    if not _comment_mentions_bandcamp(comment):
        return None
    try:
        urls = fetch_urls(mbid)
    except Exception as e:
        log.warning("MB url-rels lookup failed for %s: %s", mbid, e)
        return None
    for url in urls:
        host = (urlparse(url).hostname or "").lower()
        if host == "bandcamp.com" or host.endswith(".bandcamp.com"):
            return url
    return None


def _comment_mentions_bandcamp(comment: str) -> bool:
    if not comment:
        return False
    return "bandcamp.com" in comment.lower()
