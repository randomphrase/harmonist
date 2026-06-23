"""MusicBrainz lookups: by Bandcamp URL, plus full release fetch.

The primary lookup path is by URL relationship — given a Bandcamp album URL,
return the linked MB release MBID. This is exact, unlike the legacy
artist+title fuzzy search.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any

import musicbrainzngs

from .models import Release

# The picker lists at most this many candidate releases for a store URL — beyond
# a handful, MusicBrainz's own search is the better tool. (A single store URL
# rarely maps to more than a few releases anyway.)
MAX_URL_CANDIDATES = 5

log = logging.getLogger(__name__)


class MBError(Exception):
    """Raised on transient MB API failures (network errors, non-404 HTTP errors)."""


_USER_AGENT_RE = re.compile(r"^\s*([^/]+)/(\S+)\s*\(\s*([^)]*?)\s*\)\s*$")


def configure(user_agent: str) -> None:
    """Configure the global musicbrainzngs user-agent.

    `user_agent` is expected in the form "Name/Version ( contact )".
    """
    m = _USER_AGENT_RE.match(user_agent)
    if not m:
        raise ValueError(
            f"user_agent must look like 'Name/Version ( contact )', got: {user_agent!r}"
        )
    name, version, contact = m.group(1), m.group(2), m.group(3)
    musicbrainzngs.set_useragent(name.strip(), version.strip(), contact.strip())


def lookup_by_bandcamp_url(bandcamp_url: str) -> list[str]:
    """Return the MB release MBIDs linked to this Bandcamp album URL.

    A single Bandcamp store URL can be attached to *more than one* MB
    release — e.g. a long-form digital edition and a shorter CD mix sold
    from the same Bandcamp page. We return every linked release MBID and
    leave it to the caller (``match.best_match``) to pick the one whose
    tracklist matches the files on disk.

    Hits MB's URL relationship endpoint — if the URL isn't known to MB,
    we get a 404, which we translate to an empty list (a "no match", not
    an error).
    """
    try:
        result = musicbrainzngs.browse_urls(resource=bandcamp_url, includes=["release-rels"])
    except musicbrainzngs.ResponseError as e:
        if _is_not_found(e):
            return []
        raise MBError(f"MB ResponseError: {e}") from e
    except (musicbrainzngs.NetworkError, musicbrainzngs.AuthenticationError) as e:
        raise MBError(f"MB request failed: {e}") from e

    url_data = result.get("url") or {}
    rels = url_data.get("release-relation-list") or []
    # Dedupe (order-preserving): MB lists a release once per URL relationship,
    # so a release linked via several relationship types comes back repeatedly —
    # which otherwise shows up as duplicate rows in the picker and makes a single
    # release look like "multiple matches" to the Recheck auto-resolve.
    mbids: list[str] = []
    seen: set[str] = set()
    for rel in rels:
        release = rel.get("release") or {}
        mbid = release.get("id")
        if mbid and str(mbid) not in seen:
            seen.add(str(mbid))
            mbids.append(str(mbid))
    return mbids


def fetch_release(mbid: str) -> Release:
    """Fetch a full MB release with everything the tagger needs."""
    try:
        result = musicbrainzngs.get_release_by_id(
            mbid,
            includes=[
                "artist-credits",
                "recordings",
                "release-groups",
                "labels",
                "media",
            ],
        )
    except (
        musicbrainzngs.NetworkError,
        musicbrainzngs.ResponseError,
        musicbrainzngs.AuthenticationError,
    ) as e:
        raise MBError(f"MB request failed: {e}") from e

    release: Release = result["release"]
    return release


def fetch_release_urls(mbid: str) -> list[str]:
    """Return the list of URL relationships attached to an MB release.

    Lighter than fetch_release — only requests url-rels. Used by reconciliation
    to find Bandcamp URLs linked to an album we already have on disk.
    """
    try:
        result = musicbrainzngs.get_release_by_id(mbid, includes=["url-rels"])
    except (
        musicbrainzngs.NetworkError,
        musicbrainzngs.ResponseError,
        musicbrainzngs.AuthenticationError,
    ) as e:
        raise MBError(f"MB request failed: {e}") from e

    release = result.get("release") or {}
    rels = release.get("url-relation-list") or []
    return [r["target"] for r in rels if isinstance(r, dict) and r.get("target")]


def browse_release_group_releases(release_group_mbid: str) -> list[tuple[str, list[str]]]:
    """Return the sibling releases in a release group, each as
    ``(release_mbid, [url targets])``.

    One request (`browse_releases` with `url-rels`) yields every edition in the
    group plus its URL relationships — used to spot a mis-tag: an on-disk album
    tagged as one edition while the user owns a *different* edition (a Bandcamp
    URL) from the same group. Release groups hold a handful of editions, so the
    default page size is plenty.
    """
    try:
        result = musicbrainzngs.browse_releases(
            release_group=release_group_mbid, includes=["url-rels"], limit=100
        )
    except (
        musicbrainzngs.NetworkError,
        musicbrainzngs.ResponseError,
        musicbrainzngs.AuthenticationError,
    ) as e:
        raise MBError(f"MB request failed: {e}") from e

    out: list[tuple[str, list[str]]] = []
    for rel in result.get("release-list") or []:
        if not isinstance(rel, dict) or not rel.get("id"):
            continue
        urls = [
            r["target"]
            for r in (rel.get("url-relation-list") or [])
            if isinstance(r, dict) and r.get("target")
        ]
        out.append((str(rel["id"]), urls))
    return out


def _media_summary(media: list[dict[str, Any]]) -> str:
    """Summarise a release's media/format the way MusicBrainz does: 'CD',
    a 2-disc CD as '2{cross}CD', 'CD + Digital Media'. Empty when no format
    info is available. ({cross} = U+00D7 multiplication sign at runtime.)"""
    formats = [str(m.get("format") or "Media") for m in media]
    if not formats:
        return ""
    cross = "\N{MULTIPLICATION SIGN}"  # ASCII-safe source, real glyph at runtime
    # Counter keeps first-seen (disc) order in 3.7+.
    return " + ".join(f"{n}{cross}{fmt}" if n > 1 else fmt for fmt, n in Counter(formats).items())


def release_summary(release: Release) -> dict[str, Any]:
    """A compact, UI-ready summary of a full MB release (from `fetch_release`).

    The shape matches `mb_search.search_releases` rows so one results partial
    renders both the store-URL picker and the artist/title search.
    """
    media = release.get("medium-list") or []
    track_count = 0
    for m in media:
        tc = m.get("track-count")
        track_count += int(tc) if tc is not None else len(m.get("track-list") or [])
    label = None
    catalog = None
    for li in release.get("label-info-list") or []:
        if label is None and (name := (li.get("label") or {}).get("name")):
            label = str(name)
        if catalog is None and (cn := li.get("catalog-number")):
            catalog = str(cn)
    return {
        "id": release.get("id"),
        "title": release.get("title") or "",
        "disambiguation": (release.get("disambiguation") or "").strip(),
        "artist": (release.get("artist-credit-phrase") or "").strip(),
        "track_count": track_count or None,
        "media": _media_summary(media),
        "date": release.get("date"),
        "country": release.get("country"),
        "status": release.get("status"),
        "label": label,
        "catalog_number": catalog,
    }


def candidate_summaries_for_url(bandcamp_url: str) -> tuple[list[dict[str, Any]], int]:
    """Fresh lookup of the MB releases linked to a store URL, summarised for the
    picker. Returns (summaries capped at MAX_URL_CANDIDATES, total found) so the
    UI can note when it truncated. Re-queried on demand — never cached — so an
    edit the user made on MusicBrainz is picked up the next time they look.
    """
    mbids = lookup_by_bandcamp_url(bandcamp_url)
    summaries: list[dict[str, Any]] = []
    for mbid in mbids[:MAX_URL_CANDIDATES]:
        try:
            summaries.append(release_summary(fetch_release(mbid)))
        except MBError:
            continue
    return summaries, len(mbids)


def _is_not_found(exc: musicbrainzngs.ResponseError) -> bool:
    """Detect 404 from a musicbrainzngs ResponseError.

    Prefer the structured HTTP status on the cause; fall back to the message
    only as a safety net. The fallback matches a *standalone* 404 (e.g. in
    "HTTP Error 404: Not Found"), NOT a 404 buried inside a longer number — a
    decimal object id or an MBID could otherwise spuriously look like a 404.
    """
    cause = getattr(exc, "cause", None)
    if cause is not None and getattr(cause, "code", None) == 404:
        return True
    return re.search(r"\b404\b", str(exc)) is not None
