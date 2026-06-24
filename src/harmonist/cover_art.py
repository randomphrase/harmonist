"""Cover Art Archive fetcher with album-dir caching.

Tries the release endpoint first, falls back to the release-group endpoint
if no front cover is linked at the release level, and finally to art already
embedded in the album's audio files. Caches the result as `cover.jpg` (or
`.png`) inside the album directory so there is always a folder cover for
tools (notably Plex) that read art from disk.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from . import formats

CAA_BASE = "https://coverartarchive.org"
DEFAULT_TIMEOUT = 30.0
log = logging.getLogger(__name__)


class CoverArtError(Exception):
    pass


def cached_cover(album_dir: Path) -> Path | None:
    """Return path to a pre-existing cover.{jpg,png} in album_dir, or None."""
    for name in ("cover.jpg", "cover.png"):
        p = album_dir / name
        if p.exists():
            return p
    return None


def ensure_cover(
    album_dir: Path,
    release_mbid: str,
    release_group_mbid: str | None = None,
    size: str = "original",
    *,
    client: httpx.Client | None = None,
) -> Path | None:
    """Return the path to a cover for the album.

    If `cover.{jpg,png}` already exists in album_dir, it's returned as-is
    (treated as a manual override / cache hit). Otherwise, fetch from CAA:
    release first, then release-group fallback. If CAA has nothing (common
    for fresh / private Bandcamp releases not yet in CAA), fall back to art
    already embedded in the album's audio files. Returns None only when no
    cover is available from any source.
    """
    if cached := cached_cover(album_dir):
        return cached

    fetched = _fetch_to_disk(album_dir, release_mbid, release_group_mbid, size, client=client)
    if fetched is not None:
        return fetched

    return _extract_embedded_cover(album_dir)


def _extract_embedded_cover(album_dir: Path) -> Path | None:
    """Write a folder cover from the first audio file that carries embedded
    art. Ensures a `cover.*` exists on disk even when CAA has no match."""
    for path in sorted(p for p in album_dir.iterdir() if formats.is_supported(p)):
        result = formats.read_cover(path)
        if result is None:
            continue
        data, mime = result
        name = "cover.png" if "png" in mime.lower() else "cover.jpg"
        target = album_dir / name
        target.write_bytes(data)
        log.debug("cover: extracted embedded art from %s -> %s", path.name, target.name)
        return target
    return None


def _fetch_to_disk(
    album_dir: Path,
    release_mbid: str,
    release_group_mbid: str | None,
    size: str,
    *,
    client: httpx.Client | None,
) -> Path | None:
    suffix = "" if size == "original" else f"-{size}"
    targets = [("release", release_mbid)]
    if release_group_mbid:
        targets.append(("release-group", release_group_mbid))

    owns_client = client is None
    http = client or httpx.Client(follow_redirects=True, timeout=DEFAULT_TIMEOUT)

    try:
        for kind, mbid in targets:
            url = f"{CAA_BASE}/{kind}/{mbid}/front{suffix}"
            try:
                resp = http.get(url)
            except httpx.HTTPError as e:
                raise CoverArtError(f"CAA request failed for {url}: {e}") from e

            if resp.status_code == 404:
                log.debug("CAA: no cover for %s/%s (404)", kind, mbid)
                continue
            if resp.is_success:
                target = album_dir / _filename_for(resp)
                target.write_bytes(resp.content)
                log.debug("CAA: wrote %s (%d bytes)", target, len(resp.content))
                return target
            raise CoverArtError(f"CAA returned status {resp.status_code} for {url}")
        return None
    finally:
        if owns_client:
            http.close()


def _filename_for(resp: httpx.Response) -> str:
    ct = resp.headers.get("content-type", "").lower()
    if "png" in ct:
        return "cover.png"
    return "cover.jpg"
