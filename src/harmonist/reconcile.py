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

Pure: no globals. Caller injects the `fetch_urls` function so tests don't
need real MB lookups.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from . import formats
from . import sidecar as sidecar_mod
from .models import Sidecar
from .sidecar import CURRENT_SCHEMA_VERSION

log = logging.getLogger(__name__)


def reconcile_album(
    album_dir: Path,
    *,
    fetch_urls: Callable[[str], list[str]],
) -> Sidecar | None:
    """Inspect the album, write a sidecar, return it. None if nothing to do.

    Idempotent: if the sidecar already exists, we don't touch it.
    """
    if sidecar_mod.has_sidecar(album_dir):
        return None

    files = sorted(p for p in album_dir.iterdir() if formats.is_supported(p))
    if not files:
        return None

    mbid, comment = _read_album_id_and_comment(files)
    if not mbid:
        return None  # untagged → leave as Orphan

    bandcamp_url = _matching_bandcamp_url(mbid, comment, fetch_urls)
    now = datetime.now(UTC)

    if bandcamp_url:
        sc = Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url=bandcamp_url,
            mb_release_id=mbid,
            added_at=now,
            tagged_at=now,
        )
    else:
        sc = Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url=None,
            mb_release_id=mbid,
            added_at=now,
            tagged_at=now,
        )

    sidecar_mod.write(album_dir, sc)
    return sc


def reconcile_pending(
    album_dirs: list[Path],
    *,
    fetch_urls: Callable[[str], list[str]],
) -> dict[str, int]:
    """Reconcile a batch of album dirs. Returns a stats summary."""
    stats = {"reconciled_bandcamp": 0, "reconciled_manual": 0, "skipped": 0, "errors": 0}
    for d in album_dirs:
        try:
            sc = reconcile_album(d, fetch_urls=fetch_urls)
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


def _read_album_id_and_comment(files: list[Path]) -> tuple[str | None, str]:
    """Return (mbid, comment) from the first file that has an MBID atom."""
    for f in files:
        mbid = formats.read_album_id(f)
        if mbid:
            return mbid, formats.read_comment(f) or ""
    return None, ""


def _matching_bandcamp_url(
    mbid: str,
    comment: str,
    fetch_urls: Callable[[str], list[str]],
) -> str | None:
    """Return the Bandcamp URL to record on the sidecar, or None.

    Requires BOTH:
      - The file's ©cmt tag mentions a bandcamp.com URL (evidence of purchase).
      - MB has at least one Bandcamp URL relationship for the release.

    The sidecar's recorded URL is MB's canonical one (not whatever was in the
    comment) so subsequent sync match-by-URL is consistent.
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
