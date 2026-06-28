"""Fallback Bandcamp URL recovery for orphan albums.

When an album has no `.harmonist.json` sidecar but carries a Bandcamp URL in its
`©cmt` comment tag, we recover that URL directly — a precise `/album/` URL if
present, otherwise the artist/label-root form. Either way it's *evidence the
album is a Bandcamp purchase*, which is enough to advance it to Needs MBID
(where the user identifies the release).

No guessing: we never scrape the artist page to name-match a release from a bare
artist URL — that's exactly the kind of guess Harmonist avoids. A bare
artist-root URL is recorded as-is (the sync later links it by title); we just
don't invent a `/album/` slug we don't have.

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


def recover_store_url(album_dir: Path) -> str | None:
    """Recover the Bandcamp store URL embedded in the album's `©cmt`.

    Returns any Bandcamp URL found — a precise `/album/` or `/track/` URL if the
    comment has one, otherwise the bare artist/label-root URL (still useful: it
    marks the album a Bandcamp purchase → Needs MBID, and the sync links it by
    title later). None when the comment has no Bandcamp link at all.
    """
    files = sorted(p for p in album_dir.iterdir() if formats.is_supported(p))
    if not files:
        return None
    return extract_bandcamp_url(formats.read_comment(files[0]) or "")


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
