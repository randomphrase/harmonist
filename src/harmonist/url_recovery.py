"""Fallback Bandcamp URL recovery for orphan albums.

Used when an album has no `.harmonist.json` sidecar but has a Bandcamp
artist URL embedded in its `©cmt` comment tag. We scrape the artist's
Bandcamp page to find the album link by name match.

Tertiary fallback per design §2.1 — primary path is the bandcampsync hook
that captures URLs at download time; secondary is manual entry via the UI.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from . import formats


log = logging.getLogger(__name__)


def recover_album_url(album_dir: Path, *, client: httpx.Client | None = None) -> str | None:
    """Try to recover the public Bandcamp album URL for an orphan album.

    Returns the URL on success, None if recovery isn't possible.
    """
    files = sorted(p for p in album_dir.iterdir() if formats.is_supported(p))
    if not files:
        return None

    cmt, album_name = _read_comment_and_album(files[0])
    if not cmt or "bandcamp.com" not in cmt:
        return None

    # If the comment is already a /album/ or /track/ URL, return it directly.
    if "/album/" in cmt or "/track/" in cmt:
        return cmt

    # Otherwise it's an artist URL — scrape it to find the album link.
    if not album_name:
        album_name = album_dir.name

    return _scrape_artist_for_album(cmt, album_name, client=client)


def _read_comment_and_album(file_path: Path) -> tuple[str, str]:
    return formats.read_comment(file_path) or "", formats.read_album_title(file_path) or ""


def _scrape_artist_for_album(
    artist_url: str, album_name: str, *, client: httpx.Client | None
) -> str | None:
    owns_client = client is None
    if owns_client:
        client = httpx.Client(follow_redirects=True, timeout=30.0)

    try:
        try:
            resp = client.get(artist_url)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("URL recovery: failed to fetch %s: %s", artist_url, e)
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        target = album_name.strip().lower()
        base = httpx.URL(str(resp.url))

        for a in soup.find_all("a"):
            href = a.get("href")
            if not href or "/album/" not in href:
                continue
            text = (a.get_text() or "").strip().lower()
            title = (a.get("title") or "").strip().lower()
            if target and (target == text or target == title):
                return str(base.join(href))

        # Fallback: substring match
        for a in soup.find_all("a"):
            href = a.get("href")
            if not href or "/album/" not in href:
                continue
            text = (a.get_text() or "").strip().lower()
            title = (a.get("title") or "").strip().lower()
            if target and (target in text or target in title):
                return str(base.join(href))

        return None
    finally:
        if owns_client:
            client.close()
