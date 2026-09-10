"""Cover Art Archive fetcher with album-dir caching.

Tries the release endpoint first, falls back to the release-group endpoint
if no front cover is linked at the release level, and finally to art already
embedded in the album's audio files. Caches the result as `cover.jpg` (or
`.png`) inside the album directory so there is always a folder cover for
tools (notably Plex) that read art from disk.
"""

from __future__ import annotations

import logging
import os
import re
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

    Raises `CoverArtError` only when the archive could not be *asked* and no
    other rung served an image (#458). "I could not ask" and "there is nothing
    there" are different answers, and collapsing them into None would let a
    caller — and later #269's probe backoff — record an outage as a fact about
    the release. Every rung is tried before that error is raised, because the
    one that needs no network is the last one and it is the one an outage makes
    most useful.
    """
    if cached := cached_cover(album_dir):
        return cached

    # Remembered rather than propagated: the rung below needs no network, so an
    # archive that cannot answer must not skip it. Re-raised at the end only if
    # nothing else served an image.
    unreachable: CoverArtError | None = None
    try:
        fetched = _fetch_to_disk(album_dir, release_mbid, release_group_mbid, size, client=client)
    except CoverArtError as e:
        unreachable = e
        fetched = None
    if fetched is not None:
        return fetched

    if (embedded := _extract_embedded_cover(album_dir)) is not None:
        return embedded
    if unreachable is not None:
        raise unreachable
    return None


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

    # The first rung that failed for a reason that is NOT an answer. A 404 says
    # "this release has no front cover" and the loop moves on having learned
    # something; anything else says only that the archive could not be reached,
    # which is not grounds for abandoning the rungs below it (#458). Kept so the
    # caller can still tell the two apart once every rung is exhausted.
    failure: CoverArtError | None = None

    try:
        for kind, mbid in targets:
            url = f"{CAA_BASE}/{kind}/{mbid}/front{suffix}"
            try:
                resp = http.get(url)
            except httpx.HTTPError as e:
                # WARNING, not ERROR: another rung may still serve this album,
                # and the caller raises if none does. `exc_info` here rather than
                # on the eventual raise — this is where the traceback is.
                log.warning("CAA: %s/%s unreachable (%s)", kind, mbid, e, exc_info=True)
                failure = failure or CoverArtError(f"CAA request failed for {url}: {e}")
                continue

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
            log.warning("CAA: %s/%s returned status %d", kind, mbid, resp.status_code)
            failure = failure or CoverArtError(f"CAA returned status {resp.status_code} for {url}")
        if failure is not None:
            raise failure
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
    release_group_mbid: str | None = None,
    known: activity_store.CachedCoverArt | None = None,
    keep_if_wider_than: int | None = None,
    client: httpx.Client | None = None,
) -> activity_store.CachedCoverArt:
    """Ask the archive what front cover it has, and measure it.

    Asks the RELEASE first and falls back to its RELEASE GROUP, which is where
    the archive very often keeps the artwork — exactly as `_fetch_to_disk` has
    done since #131. Asking only the release made the album page report "no
    front cover" for albums a tagging would happily have fetched one for (#434).

    Returns the answer whatever it is — including "nothing", which is a real
    answer and the commonest one for a private Bandcamp release. The caller
    stores it; re-asking is what the user presses the refresh control for.

    **Conditional, per listing.** `If-None-Match` is sent only against the
    listing the stored etag actually came from (`known.source`): a 304 from the
    other listing would be an answer about a resource nobody asked after, and
    would keep a stale measurement. A release-group answer therefore costs the
    release's 404 again on each re-check, which is a cheap way to notice a
    release that has since gained a cover of its own.

    The second request, when it happens, is a RANGE: the listing carries no
    dimensions, so the only way to answer "is theirs bigger than mine" is to look
    at the image, and `MEASURE_BYTES` of it is enough to reach the frame header.

    The thumbnails are no use for this. `front-1200` returns 200 for a release
    whose original is 350×350 — the archive caps a thumbnail at the original
    rather than upscaling — so their presence says nothing about the size.

    `keep_if_wider_than` is the album's current best width. When the archive's
    image beats it, the whole image is fetched and cached, so the page can show
    it and a re-tag can write it — the one case where the full 200 KB–5 MB is
    worth spending, and only during a check the user asked for.

    Never raises for an ordinary "no": a 404 is an answer. A transport failure
    does propagate as `CoverArtError`, because "I could not ask" and "there is
    nothing there" must not be recorded as the same thing.
    """
    owns_client = client is None
    http = client or httpx.Client(follow_redirects=True, timeout=DEFAULT_TIMEOUT)
    now = datetime.now(UTC)
    targets = [("release", release_mbid)]
    if release_group_mbid:
        targets.append(("release-group", release_group_mbid))
    try:
        for kind, mbid in targets:
            # The etag belongs to ONE listing. Sending it at the other would ask
            # a question about the wrong resource.
            headers = (
                {"If-None-Match": known.etag}
                if known and known.etag and (known.source or "release") == kind
                else {}
            )
            try:
                listing = http.get(f"{CAA_BASE}/{kind}/{mbid}", headers=headers)
            except httpx.HTTPError as e:
                raise CoverArtError(f"CAA listing request failed for {mbid}: {e}") from e

            if listing.status_code == 304 and known is not None:
                # Unchanged since we last looked. The measurement still stands;
                # only the timestamp moves, so the page can say when that was
                # confirmed.
                return replace(known, fetched_at=now)
            if listing.status_code == 404:
                continue  # this listing has nothing; the next one may
            if not listing.is_success:
                raise CoverArtError(f"CAA returned {listing.status_code} for {mbid}")

            url = _front_url(listing)
            if url is None:
                # Images but no front — a back cover, a booklet. Not a cover to
                # offer, and the group may still have one.
                continue

            try:
                head = http.get(url, headers={"Range": f"bytes=0-{MEASURE_BYTES - 1}"})
            except httpx.HTTPError as e:
                raise CoverArtError(f"CAA image request failed for {mbid}: {e}") from e
            if not head.is_success:
                raise CoverArtError(f"CAA returned {head.status_code} for {url}")
            size = images.dimensions(head.content)
            if (
                size is not None
                and keep_if_wider_than is not None
                and size.width > keep_if_wider_than
                and cached_image(release_mbid) is None
            ):
                _fetch_and_cache(http, release_mbid, url, head.headers.get("content-type"))
            return activity_store.CachedCoverArt(
                fetched_at=now,
                etag=listing.headers.get("etag"),
                image_url=url,
                width=size.width if size else None,
                height=size.height if size else None,
                # The WHOLE image's length, off the range response, not what was
                # read: `content-range` states it, and a server that ignored the
                # range states it in `content-length` instead.
                length=_total_length(head),
                mime=head.headers.get("content-type"),
                source=kind,
            )
        # Neither listing has a front cover. Recorded so the next check does not
        # ask again — the timestamp says when that was established.
        return activity_store.CachedCoverArt(fetched_at=now)
    finally:
        if owns_client:
            http.close()


def fetch_image(release_mbid: str, url: str, *, client: httpx.Client | None = None) -> Path | None:
    """Fetch the archive's image for this release and keep it. Where it went, or
    None if the cache is switched off.

    For the image that LOST (#448). A losing candidate is deliberately never
    downloaded — bigger is the only thing Harmonist can measure, and spending
    megabytes on a picture nobody will use is the rule #276 set — but bigger is
    not the same as better, and this is a user asking to see the other one.

    Takes the URL rather than looking it up, because the caller has the stored
    answer in hand and re-reading it here would be a second SQLite hit to learn
    something already known.

    Raises `CoverArtError` where `_fetch_and_cache` swallows and logs. The
    difference is who asked: that one runs inside a check whose real answer is
    the measurement, so losing the picture is a footnote. This one IS the
    request, and somebody is waiting to look at the result.
    """
    owns_client = client is None
    http = client or httpx.Client(follow_redirects=True, timeout=DEFAULT_TIMEOUT)
    try:
        try:
            resp = http.get(url)
        except httpx.HTTPError as e:
            raise CoverArtError(f"CAA image request failed for {release_mbid}: {e}") from e
        if not resp.is_success:
            raise CoverArtError(f"CAA returned {resp.status_code} for {url}")
        return cache_image(release_mbid, resp.content, resp.headers.get("content-type"))
    finally:
        if owns_client:
            http.close()


def _fetch_and_cache(http: httpx.Client, release_mbid: str, url: str, mime: str | None) -> None:
    """Pull the whole image and keep it. Best-effort: a failure here loses the
    picture, not the measurement that was the point of the check."""
    try:
        full = http.get(url)
    except httpx.HTTPError:
        log.warning("could not fetch the archive image for %s", release_mbid, exc_info=True)
        return
    if not full.is_success:
        log.warning("archive image for %s returned %s", release_mbid, full.status_code)
        return
    cache_image(release_mbid, full.content, mime or full.headers.get("content-type"))


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


# ---------------------------------------------------------------------------
# The candidate cache (#276)
# ---------------------------------------------------------------------------
#
# The archive's image, kept so it can be SHOWN beside the album's own and
# written if it wins. One file per release, named for the release — not
# content-addressed like `artwork_store`, because there is nothing to
# deduplicate: two releases sharing a cover is not a thing that happens, and a
# release has exactly one current front cover.
#
# The deeper difference from `artwork_store` is what eviction costs. That store
# holds the ONLY copy of something the user had, so dropping a file breaks a
# promise (#408). These are copies of something the archive still has, so
# dropping one costs a re-fetch and nothing else — which is why this needs no
# per-album retention, no protected set, and no undo semantics. Deleting the
# whole directory is safe at any moment.

_caa_root: Path | None = None


def configure_cache(root: Path | None) -> None:
    """Point the candidate cache at `root` (created on demand). None disables
    it — every call becomes a no-op, and the archive's image simply is not
    shown."""
    global _caa_root
    _caa_root = root


def cache_image(release_mbid: str, data: bytes, mime: str | None) -> Path | None:
    """Keep the archive's image for this release. Returns where, or None.

    Best-effort by design, like `artwork_store.keep`: a cache that cannot be
    written must not fail the check the user asked for. They lose a thumbnail,
    not an answer.
    """
    root = _caa_root
    if root is None or not _is_mbid(release_mbid):
        return None
    path = root / f"{release_mbid}{'.png' if mime and 'png' in mime.lower() else '.jpg'}"
    try:
        root.mkdir(parents=True, exist_ok=True)
        # Temp file then rename, so a crash cannot leave a half-image behind a
        # name that claims to be complete — as everywhere else Harmonist writes.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError:
        log.exception("could not cache the Cover Art Archive image for %s", release_mbid)
        return None
    return path


def cached_image(release_mbid: str) -> Path | None:
    """Where this release's archive image is held locally, or None."""
    root = _caa_root
    if root is None or not _is_mbid(release_mbid):
        return None
    for suffix in (".jpg", ".png"):
        path = root / f"{release_mbid}{suffix}"
        if path.exists():
            return path
    return None


def _is_mbid(value: str) -> bool:
    """Guard the path join: the id reaches here from a sidecar, and a value
    carrying a separator must never become a filename.

    A CHARACTER guard, not a format one. Real release ids are UUIDs, but
    Harmonist's own fixtures and demo library use short readable ids, and a
    regex demanding 36 hex characters would silently disable the cache for every
    one of them — a check that appears to work and quietly stores nothing. What
    matters here is only that the value cannot escape the directory.
    """
    return bool(_SAFE_ID.fullmatch(value))


#: An id safe to use as a filename: no separators, no traversal, bounded.
_SAFE_ID = re.compile(r"(?!\.)[A-Za-z0-9._-]{1,64}")


def _filename_for(resp: httpx.Response) -> str:
    ct = resp.headers.get("content-type", "").lower()
    if "png" in ct:
        return "cover.png"
    return "cover.jpg"
