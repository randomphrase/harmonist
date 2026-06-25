"""Fallback Bandcamp URL recovery for orphan albums.

When an album has no `.harmonist.json` sidecar but carries a Bandcamp
**album/track** URL in its `©cmt` comment tag, we recover that URL directly.

No guessing: if the comment holds only an artist/label-root URL (no specific
release path), we recover nothing rather than scrape the artist page and
name-match a release — that's exactly the kind of guess Harmonist avoids. The
user can still set the URL by hand in the UI.

Tertiary fallback per design §2.1 — the primary path is the bandcampsync hook
that captures URLs at download time; the secondary is manual entry via the UI.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from . import formats

log = logging.getLogger(__name__)

# Bandcamp embeds the link as prose in the comment tag, e.g.
# "Visit https://artist.bandcamp.com/album/x" — so we extract the URL rather
# than treating the whole comment as one.
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def recover_album_url(album_dir: Path) -> str | None:
    """Recover the Bandcamp album/track URL embedded in the album's `©cmt`.

    Returns the URL only when it points at a specific release (`/album/…` or
    `/track/…`). An artist/label-root URL — or no Bandcamp URL at all — yields
    None; we don't guess which release a bare artist page refers to.
    """
    files = sorted(p for p in album_dir.iterdir() if formats.is_supported(p))
    if not files:
        return None

    url = extract_bandcamp_url(formats.read_comment(files[0]) or "")
    if url and ("/album/" in url or "/track/" in url):
        return url
    return None


def extract_bandcamp_url(comment: str) -> str | None:
    """Pull the first bandcamp.com URL out of a comment tag, stripping any
    surrounding prose ("Visit …") and trailing punctuation. Returns None when
    the comment has no Bandcamp link.
    """
    for match in _URL_RE.finditer(comment or ""):
        url = match.group(0).rstrip(".,);]>\"'")
        if "bandcamp.com" in url.lower():
            return url
    return None
