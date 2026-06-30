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
from urllib.parse import urlparse

from . import formats

log = logging.getLogger(__name__)


def album_slug(url: str | None) -> str | None:
    """Extract the Bandcamp release slug from a URL, ignoring the subdomain.

    `https://echospace313.bandcamp.com/album/dimensional-space-remastered-by-pole`
    → `album/dimensional-space-remastered-by-pole`

    The slug is Bandcamp's stable per-release handle: minted once at release
    and effectively immutable, even when the artist renames the band or
    re-letters the title. The *subdomain*, by contrast, varies — the same
    release is often cross-listed under both a label page and an artist page
    (e.g. `echospacedetroit` vs the artist's own subdomain), which defeats a
    whole-URL compare. Matching on the slug bridges that.

    Returns `None` for URLs that aren't an `/album/<slug>` or `/track/<slug>`
    shape (e.g. a bare `artist.bandcamp.com` landing page embedded in tags) —
    those carry no release identity and must never match.

    The item-type segment (`album`/`track`) is kept in the key so an album and
    a track that happen to share a slug don't collide.
    """
    if not url:
        return None
    try:
        path = urlparse(url).path
    except ValueError:
        return None
    parts = [seg for seg in path.split("/") if seg]
    if len(parts) >= 2 and parts[-2] in ("album", "track"):
        return f"{parts[-2]}/{parts[-1].lower()}"
    return None


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
