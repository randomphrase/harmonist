"""Cover Art Archive fetcher with album-dir caching.

Tries the release endpoint first, falls back to the release-group endpoint
if no front cover is linked at the release level, and finally to art already
embedded in the album's audio files. Caches the result as `cover.jpg` (or
`.png`) inside the album directory so there is always a folder cover for
tools (notably Plex) that read art from disk.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx

from . import activity_store, album_files, audit, formats, images

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
    for path in album_files.audio_files(album_dir):
        result = formats.read_cover(path)
        if result is None:
            continue
        data, mime = result
        name = "cover.png" if "png" in mime.lower() else "cover.jpg"
        target = album_dir / name
        # Writing into the user's album dir, and possibly over an existing
        # cover.* they put there themselves — audited like any other file
        # overwrite (#88). `overwrote` distinguishes creating from replacing.
        overwrote = target.exists()
        target.write_bytes(data)
        audit.record(
            "cover.write", album=album_dir, file=target.name, source="embedded", overwrote=overwrote
        )
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
                overwrote = target.exists()
                target.write_bytes(resp.content)
                audit.record(
                    "cover.write",
                    album=album_dir,
                    file=target.name,
                    source="caa",
                    mbid=mbid,
                    bytes=len(resp.content),
                    overwrote=overwrote,
                )
                log.debug("CAA: wrote %s (%d bytes)", target, len(resp.content))
                return target
            raise CoverArtError(f"CAA returned status {resp.status_code} for {url}")
        return None
    finally:
        if owns_client:
            http.close()


#: How much of an image to read to find its size. The dimensions live in the
#: JPEG frame header, which sits behind however much EXIF and colour profile the
#: encoder wrote: on a release measured from the dogfood library the header was
#: past 16 KB and inside 64 KB, on a 100 KB file. Generous, and still a fraction
#: of the 200 KB–5 MB the whole image would cost.
MEASURE_BYTES = 65536


def check_front(
    release_mbid: str,
    *,
    known: activity_store.CachedCoverArt | None = None,
    client: httpx.Client | None = None,
) -> activity_store.CachedCoverArt:
    """Ask the archive what front cover it has for `release_mbid`, and measure it.

    Returns the answer whatever it is — including "nothing", which is a real
    answer and the commonest one for a private Bandcamp release. The caller
    stores it; re-asking is what the user presses the refresh control for.

    **Two requests at most, and usually one and a bit.** The listing is
    revalidated with `If-None-Match` when `known` carries an etag, and
    coverartarchive.org answers a match with a 304 and no body — so a re-check of
    an unchanged release costs one request and no transfer, and the previous
    measurement is returned unchanged. Only a listing that has actually moved
    costs the second request.

    That second request is a RANGE, not the whole image: the listing carries no
    dimensions, so the only way to answer "is theirs bigger than mine" is to look
    at the image, and `MEASURE_BYTES` of it is enough to reach the frame header.
    A server that ignores the range simply sends more than was asked for, which
    still works.

    The thumbnails are no use for this. `front-1200` returns 200 for a release
    whose original is 350×350 — the archive caps a thumbnail at the original
    rather than upscaling — so their presence says nothing about the size.

    Never raises for an ordinary "no": a 404 is an answer. A transport failure
    does propagate as `CoverArtError`, because "I could not ask" and "there is
    nothing there" must not be recorded as the same thing.
    """
    owns_client = client is None
    http = client or httpx.Client(follow_redirects=True, timeout=DEFAULT_TIMEOUT)
    now = datetime.now(UTC)
    try:
        headers = {"If-None-Match": known.etag} if known and known.etag else {}
        try:
            listing = http.get(f"{CAA_BASE}/release/{release_mbid}", headers=headers)
        except httpx.HTTPError as e:
            raise CoverArtError(f"CAA listing request failed for {release_mbid}: {e}") from e

        if listing.status_code == 304 and known is not None:
            # Unchanged since we last looked. The measurement still stands; only
            # the timestamp moves, so the page can say when that was confirmed.
            return replace(known, fetched_at=now)
        if listing.status_code == 404:
            return activity_store.CachedCoverArt(fetched_at=now)
        if not listing.is_success:
            raise CoverArtError(f"CAA returned {listing.status_code} for {release_mbid}")

        url = _front_url(listing)
        etag = listing.headers.get("etag")
        if url is None:
            # A listing with images but no front — a back cover, a booklet. Not
            # a cover to offer, and remembered so it is not asked again.
            return activity_store.CachedCoverArt(fetched_at=now, etag=etag)

        try:
            head = http.get(url, headers={"Range": f"bytes=0-{MEASURE_BYTES - 1}"})
        except httpx.HTTPError as e:
            raise CoverArtError(f"CAA image request failed for {release_mbid}: {e}") from e
        if not head.is_success:
            raise CoverArtError(f"CAA returned {head.status_code} for {url}")
        size = images.dimensions(head.content)
        return activity_store.CachedCoverArt(
            fetched_at=now,
            etag=etag,
            image_url=url,
            width=size.width if size else None,
            height=size.height if size else None,
            # The WHOLE image's length, off the range response, not what was
            # read: `content-range` states it, and a server that ignored the
            # range states it in `content-length` instead.
            length=_total_length(head),
            mime=head.headers.get("content-type"),
        )
    finally:
        if owns_client:
            http.close()


def _front_url(listing: httpx.Response) -> str | None:
    """The URL of the release's front cover, or None if it has no front image."""
    try:
        payload = listing.json()
    except ValueError:
        return None
    images_ = payload.get("images") if isinstance(payload, dict) else None
    if not isinstance(images_, list):
        return None
    for image in images_:
        if isinstance(image, dict) and image.get("front") and isinstance(image.get("image"), str):
            # https, because the listing states http and the browser this ends
            # up in will be on a page served over TLS.
            return str(image["image"]).replace("http://", "https://", 1)
    return None


def _total_length(resp: httpx.Response) -> int | None:
    """The full image's byte length, however the server answered the range."""
    content_range = resp.headers.get("content-range", "")
    if "/" in content_range:
        total = content_range.rsplit("/", 1)[-1].strip()
        if total.isdigit():
            return int(total)
    length = resp.headers.get("content-length")
    return int(length) if length and length.isdigit() and resp.status_code == 200 else None


def _filename_for(resp: httpx.Response) -> str:
    ct = resp.headers.get("content-type", "").lower()
    if "png" in ct:
        return "cover.png"
    return "cover.jpg"
