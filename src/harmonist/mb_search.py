"""MB name-based search — used as a manual-ingest helper only.

The primary lookup path for Bandcamp albums is `mb_lookup.lookup_by_bandcamp_url`
(exact, by URL relationship). This module is the fallback for non-Bandcamp
ingests, where the user has artist + title text and we surface candidate
releases for them to pick from.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import musicbrainzngs

from . import barcodes, mb_lookup
from .models import Release

log = logging.getLogger(__name__)


class MBSearchError(Exception):
    pass


def matches_barcode(release: Release, evidence: barcodes.Evidence) -> bool:
    """Verify the identifier only; names and medium format are review evidence."""
    expected = barcodes.normalise(evidence.barcode)
    return expected is not None and barcodes.normalise(release.get("barcode") or "") == expected


def search_barcode(evidence: barcodes.Evidence) -> tuple[list[dict[str, Any]], int]:
    """One bounded, uncached search; verify barcodes instead of trusting score.

    Query every equivalent spelling. A truncated answer cannot prove uniqueness;
    its total remains the unfiltered count so callers offer choices instead.
    """
    spellings = barcodes.variants(evidence.barcode)
    if not spellings:
        return [], 0
    query = "barcode:(" + " OR ".join(spellings) + ")"
    try:
        response = musicbrainzngs.search_releases(query=query, limit=100)
    except (
        musicbrainzngs.NetworkError,
        musicbrainzngs.ResponseError,
        musicbrainzngs.AuthenticationError,
    ) as exc:
        raise MBSearchError(f"MusicBrainz barcode lookup failed: {exc}") from exc
    releases = response.get("release-list", [])
    total = int(response["release-count"])
    results: dict[str, dict[str, Any]] = {}
    for release in releases:
        if matches_barcode(release, evidence):
            results[release["id"]] = mb_lookup.release_summary(release)
    return list(results.values()), total if total > len(releases) else len(results)


def search_releases(artist: str, title: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search MB for releases matching `artist` + `title`.

    Returns a list of dicts with the fields the manual-ingest UI needs:
    id, title, artist, date, country, status, track_count, label, catalog_number.
    """
    artist = (artist or "").strip()
    title = (title or "").strip()
    if not artist and not title:
        return []

    parts = []
    if artist:
        parts.append(f'artist:"{_escape(artist)}"')
    if title:
        parts.append(f'release:"{_escape(title)}"')
    query = " AND ".join(parts)

    try:
        result = musicbrainzngs.search_releases(query=query, limit=limit)
    except (
        musicbrainzngs.NetworkError,
        musicbrainzngs.ResponseError,
        musicbrainzngs.AuthenticationError,
    ) as e:
        raise MBSearchError(f"MB search failed: {e}") from e

    out: list[dict[str, Any]] = []
    for rel in result.get("release-list", []):
        out.append(
            {
                "id": rel.get("id"),
                "title": rel.get("title"),
                "disambiguation": (rel.get("disambiguation") or "").strip(),
                "artist": rel.get("artist-credit-phrase") or _extract_artist(rel),
                "date": rel.get("date"),
                "country": rel.get("country"),
                "status": rel.get("status"),
                "track_count": rel.get("medium-track-count"),
                "media": mb_lookup._media_summary(rel.get("medium-list") or []),
                "label": _first_label(rel),
                "catalog_number": _first_catalog(rel),
            }
        )
    return out


_LUCENE_RESERVED = re.compile(r'([\\"])')


def _escape(s: str) -> str:
    """Escape backslash + quote for use inside a Lucene quoted string field."""
    return _LUCENE_RESERVED.sub(r"\\\1", s)


def _extract_artist(release: Release) -> str:
    parts = []
    for ac in release.get("artist-credit") or []:
        if isinstance(ac, str):
            parts.append(ac)
        elif isinstance(ac, dict):
            parts.append(ac.get("name") or ac.get("artist", {}).get("name", ""))
    return "".join(parts).strip()


def _first_label(release: Release) -> str | None:
    for li in release.get("label-info-list") or []:
        if name := li.get("label", {}).get("name"):
            return str(name)
    return None


def _first_catalog(release: Release) -> str | None:
    for li in release.get("label-info-list") or []:
        if catnum := li.get("catalog-number"):
            return str(catnum)
    return None
