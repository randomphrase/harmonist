"""Optional MusicBrainz contributions, derived separately from tag updates.

Release observations can be shared, but eligibility and conclusions belong to
one local copy. A purchase link alone says nothing about where its files came
from. No function here writes music, sidecars, or persisted status flags.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from . import mb_cache, mb_lookup
from .models import Album, Release


@dataclass(frozen=True)
class Observation:
    mbid: str
    formats: tuple[str, ...]
    urls: frozenset[str]
    checked_at: datetime | None


@dataclass(frozen=True)
class Assessment:
    eligible: bool = False
    private: bool = False
    store_url: str | None = None
    media_mismatch: bool | None = None
    missing_url: bool | None = None
    observation: Observation | None = None

    @property
    def has_findings(self) -> bool:
        return self.media_mismatch is True or (
            self.media_mismatch is False and self.missing_url is True
        )

    @property
    def unchecked(self) -> bool:
        return self.eligible and (
            self.observation is None
            or self.media_mismatch is None
            or (not self.private and self.store_url is None)
        )


def release_url(url: str | None) -> str | None:
    """Conservative release URL equality; never match a slug across hosts.

    HTTP/HTTPS, a trailing slash, and tracking query/fragment do not distinguish
    Bandcamp album/track pages. Other path shapes cannot identify a download.
    """
    if not url:
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None  # malformed input cannot establish release identity
    path = parts.path.rstrip("/")
    segments = path.split("/")
    if (
        parts.scheme not in {"http", "https"}
        or not host
        or parts.username
        or parts.password
        or port not in {None, 80, 443}
        or len(segments) != 3
        or segments[1] not in {"album", "track"}
        or not segments[2]
    ):
        return None
    return urlunsplit(("https", host.lower().removeprefix("www."), path, "", ""))


def assess(album: Album) -> Assessment:
    sc = album.sidecar
    if sc is None or not sc.mb_release_id:
        return Assessment()
    if not sc.bandcamp_downloaded and not album.bandcamp_comment_urls:
        return Assessment()
    private = bool(sc.bandcamp and sc.bandcamp.is_private)
    comments = {u for raw in album.bandcamp_comment_urls if (u := release_url(raw))}
    # An actual download records its own store URL. Otherwise prefer precise
    # file evidence over an MB-derived URL that may describe a nearby edition.
    url = release_url(sc.store_url) if sc.bandcamp_downloaded else None
    if url is None:
        if len(comments) == 1:
            url = next(iter(comments))
        elif not comments:
            url = release_url(sc.store_url)
    observed = album.contribution_observation
    if observed is not None and observed.mbid != sc.mb_release_id:
        observed = None  # rematching never carries an old release's findings
    media: bool | None = None
    missing: bool | None = None
    if observed is not None:
        if any(f and f != "Digital Media" for f in observed.formats):
            media = True
        elif observed.formats and all(observed.formats):
            media = False
        if url and not private:
            missing = url not in observed.urls
    return Assessment(True, private, url, media, missing, observed)


def observe(album: Album, release: Release, checked_at: datetime | None) -> None:
    """Use a successful full release fetch, whose includes request url-rels.

    A missing url-relation-list in that response means no relationships. An
    absent cache row is handled by warm(), never by calling this with {}.
    """
    album.contribution_observation = Observation(
        str(release["id"]),
        tuple(str(m.get("format") or "") for m in (release.get("medium-list") or [])),
        frozenset(
            url
            for rel in (release.get("url-relation-list") or [])
            if (url := release_url(rel.get("target")))
        ),
        checked_at,
    )


def warm(album: Album) -> None:
    """Rebuild from the durable current-includes cache without network I/O."""
    if not assess(album).eligible:
        return
    assert album.sidecar is not None and album.sidecar.mb_release_id is not None
    mbid = album.sidecar.mb_release_id
    snapshot = mb_cache.stored_release_snapshot(mbid)
    if snapshot is not None:
        observe(album, snapshot.payload, snapshot.fetched_at)


def digital_editions(
    releases: list[Release], assessment: Assessment
) -> tuple[list[dict[str, Any]], int]:
    """Digital candidates, never matches; retain uncertainty about missing media."""
    editions = []
    unknown = 0
    for release in releases:
        formats = [m.get("format") for m in (release.get("medium-list") or [])]
        if any(f and f != "Digital Media" for f in formats):
            continue
        if not formats or not all(formats):
            unknown += 1
            continue
        urls = {
            url
            for rel in (release.get("url-relation-list") or [])
            if (url := release_url(rel.get("target")))
        }
        editions.append(
            {
                **mb_lookup.release_summary(release),
                "store_linked": (
                    assessment.store_url in urls
                    if assessment.store_url and not assessment.private
                    else None
                ),
            }
        )
    return editions, unknown
