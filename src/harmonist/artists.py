"""Artist browsing derived from the scanner snapshot, never a second catalogue.

Consistent, aligned album credits establish identity. Missing IDs can inherit
that identity only within one identified release and with agreeing names.
Unidentified names remain scoped to their album; spelling alone cannot merge
people or bind an NFO.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlencode

from .formats.types import ScanFields
from .models import Album, AlbumArtist

_MBID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE)


def mbid(value: str) -> str | None:
    """Accept an actual identifier, not a name or an inferred MusicBrainz URL."""
    value = value.strip()
    return value.lower() if _MBID.fullmatch(value) else None


def album_credits(fields: Sequence[ScanFields]) -> tuple[AlbumArtist, ...]:
    """Only agreement across every readable track can identify album artists."""
    if not fields or any(f.unreadable for f in fields):
        return ()
    observed_names = {
        tuple(n.strip() for n in f.album_artist_names) or ((f.album_artist or "").strip(),)
        for f in fields
    }
    if len(observed_names) != 1:
        return ()
    names = observed_names.pop()
    if not names or any(not n for n in names):
        return ()
    observed_ids = {
        tuple(mbid(i) for i in f.album_artist_ids) for f in fields if f.album_artist_ids
    }
    ids = next(iter(observed_ids)) if len(observed_ids) == 1 else ()
    aligned = len(ids) == len(names) and all(ids)
    missing = sum(not f.album_artist_ids for f in fields)
    if missing and aligned:
        # Absence is not disagreement. Scope the available artist IDs to one
        # release carried by EVERY file; never attach a name-only album to an
        # artist identified somewhere else in the library.
        releases = {mbid(f.album_id or "") for f in fields}
        aligned = len(releases) == 1 and None not in releases
    sorts = {f.album_artist_sort for f in fields}
    sort = next(iter(sorts)) if len(sorts) == 1 and len(names) == 1 else None
    return tuple(
        AlbumArtist(name, ids[i] if aligned else None, sort, missing)
        for i, name in enumerate(names)
    )


def alphabetical(value: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", value.casefold()) if not unicodedata.combining(c)
    )


@dataclass(frozen=True)
class Artist:
    key: str
    name: str
    mbid: str | None
    sort_name: str | None
    albums: tuple[Album, ...]

    @property
    def unidentified_albums(self) -> tuple[Album, ...]:
        return tuple(
            album
            for album in self.albums
            if self.mbid is None
            or any(c.mbid == self.mbid and c.missing_id_count for c in album.artist_credits)
        )


def build_index(albums: Sequence[Album]) -> list[Artist]:
    """Share identified collaborations without coalescing homonymous artists."""
    credits: dict[str, AlbumArtist] = {}
    groups: dict[str, dict[str, Album]] = {}
    for album in sorted(albums, key=lambda a: a.id):
        for credit in album.artist_credits or (AlbumArtist(album.artist or "Unknown artist"),):
            key = (
                credit.mbid
                or "local-"
                + hashlib.sha256((album.id + "\0" + credit.name).encode()).hexdigest()[:24]
            )
            credits.setdefault(key, credit)
            groups.setdefault(key, {})[album.id] = album
    result = [
        Artist(
            key,
            c.name,
            c.mbid,
            c.sort_name,
            tuple(sorted(groups[key].values(), key=lambda a: (alphabetical(a.title), a.id))),
        )
        for key, c in credits.items()
    ]
    return sorted(result, key=lambda a: (alphabetical(a.name), a.key))


def index_url(page: int = 1, q: str = "") -> str:
    return "/?" + urlencode({"tab": "artists", "artist_page": page, "artist_q": q})


def detail_url(
    key: str,
    *,
    page: int = 1,
    artist_page: int = 1,
    artist_q: str = "",
    nfo: str | None = None,
) -> str:
    params = {"page": str(page), "artist_page": str(artist_page), "artist_q": artist_q}
    if nfo:
        params["nfo"] = nfo
    return f"/artist/{key}?{urlencode(params)}"


def page_vars(albums: Sequence[Album], page: int = 1, q: str | None = None) -> dict[str, object]:
    all_artists = build_index(albums)
    query = (q or "").strip()[:100]
    rows = [a for a in all_artists if alphabetical(query) in alphabetical(a.name)]
    pages = max(1, (len(rows) + 29) // 30)
    page = min(max(1, page), pages)
    return {
        "rows": rows[(page - 1) * 30 : page * 30],
        "q": query,
        "page": page,
        "pages": pages,
        "total": len(rows),
        "count": len(all_artists),
        "url": index_url(page, query),
        "previous": index_url(page - 1, query) if page > 1 else None,
        "next": index_url(page + 1, query) if page < pages else None,
    }
