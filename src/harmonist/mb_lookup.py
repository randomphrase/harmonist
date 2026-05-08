"""MusicBrainz lookups: by Bandcamp URL, plus full release fetch.

The primary lookup path is by URL relationship — given a Bandcamp album URL,
return the linked MB release MBID. This is exact, unlike the legacy
artist+title fuzzy search.
"""
from __future__ import annotations

import logging
import re

import musicbrainzngs


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


def lookup_by_bandcamp_url(bandcamp_url: str) -> str | None:
    """Return the MB release MBID linked to this Bandcamp album URL, or None.

    Hits MB's URL relationship endpoint — if the URL isn't known to MB,
    we get a 404, which we translate to None (a "no match", not an error).
    """
    try:
        result = musicbrainzngs.browse_urls(
            resource=bandcamp_url, includes=["release-rels"]
        )
    except musicbrainzngs.ResponseError as e:
        if _is_not_found(e):
            return None
        raise MBError(f"MB ResponseError: {e}") from e
    except (musicbrainzngs.NetworkError, musicbrainzngs.AuthenticationError) as e:
        raise MBError(f"MB request failed: {e}") from e

    url_data = result.get("url") or {}
    rels = url_data.get("release-relation-list") or []
    for rel in rels:
        release = rel.get("release") or {}
        if mbid := release.get("id"):
            return mbid
    return None


def fetch_release(mbid: str) -> dict:
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
    except (musicbrainzngs.NetworkError, musicbrainzngs.ResponseError, musicbrainzngs.AuthenticationError) as e:
        raise MBError(f"MB request failed: {e}") from e

    return result["release"]


def _is_not_found(exc: musicbrainzngs.ResponseError) -> bool:
    """Detect 404 from a musicbrainzngs ResponseError."""
    cause = getattr(exc, "cause", None)
    if cause is not None and getattr(cause, "code", None) == 404:
        return True
    return "404" in str(exc)
