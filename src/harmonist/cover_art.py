"""Cover Art Archive fetcher with album-dir caching.

Tries the release endpoint first, falls back to the release-group endpoint
if no front cover is linked at the release level. Caches the result as
`cover.jpg` (or `.png`) inside the album directory.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

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
    release first, then release-group fallback. Returns None if no cover
    is available at either source.
    """
    if cached := cached_cover(album_dir):
        return cached

    return _fetch_to_disk(album_dir, release_mbid, release_group_mbid, size, client=client)


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
    if owns_client:
        client = httpx.Client(follow_redirects=True, timeout=DEFAULT_TIMEOUT)

    try:
        for kind, mbid in targets:
            url = f"{CAA_BASE}/{kind}/{mbid}/front{suffix}"
            try:
                resp = client.get(url)
            except httpx.HTTPError as e:
                raise CoverArtError(f"CAA request failed for {url}: {e}") from e

            if resp.status_code == 404:
                log.info("CAA: no cover for %s/%s (404)", kind, mbid)
                continue
            if resp.is_success:
                target = album_dir / _filename_for(resp)
                target.write_bytes(resp.content)
                log.info("CAA: wrote %s (%d bytes)", target, len(resp.content))
                return target
            raise CoverArtError(f"CAA returned status {resp.status_code} for {url}")
        return None
    finally:
        if owns_client:
            client.close()


def _filename_for(resp: httpx.Response) -> str:
    ct = resp.headers.get("content-type", "").lower()
    if "png" in ct:
        return "cover.png"
    return "cover.jpg"
