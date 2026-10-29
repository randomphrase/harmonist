"""Artist browsing derived from the scanner snapshot, never a second catalogue.

Consistent, aligned album credits establish identity. Missing IDs can inherit
that identity only within one identified release and with agreeing names.
Unidentified credits are listed as albums needing artist IDs; spelling alone
cannot create an artist page or bind an NFO.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from .formats.types import ScanFields
from .models import Album, AlbumArtist, AlbumState

if TYPE_CHECKING:
    from .artist_nfo import Inventory

_MBID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE)

# Official special-purpose artists, identified by MBID rather than display name.
# https://musicbrainz.org/doc/Style/Unknown_and_untitled/Special_purpose_artist
SPECIAL_ARTIST_IDS = frozenset(
    {
        "89ad4ac3-39f7-470e-963a-56509c546377",  # Various Artists
        "125ec42a-7229-4250-afc5-e057484327fe",  # [unknown]
        "f731ccc4-e22a-43af-a747-64213329e088",  # [anonymous]
        "33cf029c-63b0-41a0-9855-be2a3665fb3b",  # [data]
        "314e1c25-dde7-4e4d-b2f4-0a7b9f7c56dc",  # [dialogue]
        "eec63d3c-3b81-4ad4-b1e4-7c147d4d2b61",  # [no artist]
        "9be7f096-97ec-4615-8957-8d40b5dcbc41",  # [traditional]
        "7e84f845-ac16-41fe-9ff8-df12eb32af55",  # MusicBrainz Test Artist
    }
)


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

    # Derived from NFO contents on the detail page, never persisted. The artist's
    # name remains the tag credit even when an NFO supplies a different name.
    nfo_paths: tuple[Path, ...] = ()
    artwork_path: Path | None = None


def build_index(albums: Sequence[Album]) -> list[Artist]:
    """Share identified collaborations without coalescing homonymous artists."""
    credits: dict[str, AlbumArtist] = {}
    groups: dict[str, dict[str, Album]] = {}
    for album in sorted(albums, key=lambda a: a.id):
        for credit in album.artist_credits:
            key = mbid(credit.mbid or "")
            if key is None or key in SPECIAL_ARTIST_IDS:
                continue
            credits.setdefault(key, credit)
            groups.setdefault(key, {})[album.id] = album
    result = [
        Artist(
            key,
            c.name,
            key,
            c.sort_name,
            tuple(sorted(groups[key].values(), key=lambda a: (alphabetical(a.title), a.id))),
        )
        for key, c in credits.items()
    ]
    return sorted(result, key=lambda a: (alphabetical(a.name), a.key))


def library_count(albums: Sequence[Album]) -> int:
    """Global count for the consolidated status poll, without sorting or I/O."""
    return len(
        {
            identity
            for album in albums
            if album.state in {AlbumState.COMPLETE, AlbumState.INCOMPLETE}
            for credit in album.artist_credits
            if (identity := mbid(credit.mbid or "")) and identity not in SPECIAL_ARTIST_IDS
        }
    )


def index_url(
    page: int = 1,
    q: str = "",
    *,
    nfo: str | None = None,
    artwork: str | None = None,
    new: bool = False,
) -> str:
    params = {"artist_page": str(page), "artist_q": q}
    if nfo:
        params["nfo"] = nfo
    if artwork:
        params["artwork"] = artwork
    if new:
        params["new"] = "true"
    return "/artists?" + urlencode(params)


def detail_url(
    key: str,
    *,
    page: int = 1,
    artist_page: int = 1,
    artist_q: str = "",
    artist_nfo: str | None = None,
    artist_artwork: str | None = None,
    artist_new: bool = False,
) -> str:
    params = {"page": str(page), "artist_page": str(artist_page), "artist_q": artist_q}
    if artist_nfo:
        params["artist_nfo"] = artist_nfo
    if artist_artwork:
        params["artist_artwork"] = artist_artwork
    if artist_new:
        params["artist_new"] = "true"
    return f"/artist/{key}?{urlencode(params)}"


def page_vars(
    albums: Sequence[Album],
    page: int = 1,
    q: str | None = None,
    *,
    inventory: Inventory | None = None,
    nfo: str | None = None,
    artwork: str | None = None,
    new: bool = False,
    now: datetime | None = None,
) -> dict[str, object]:
    all_artists = build_index(albums)
    if inventory is not None:
        all_artists = [
            replace(
                a,
                nfo_paths=inventory.for_artist(a.mbid).candidates,
                artwork_path=inventory.image_for(a.mbid),
            )
            for a in all_artists
        ]
    nfo = nfo if nfo in {"with", "missing", "conflict"} else None
    artwork = artwork if artwork in {"with", "missing"} else None
    now = now or datetime.now(UTC)
    recent = {
        a.key
        for a in all_artists
        if any(
            album.sidecar
            and album.sidecar.added_at is not None
            and timedelta(0) <= now - album.sidecar.added_at < timedelta(days=7)
            for album in a.albums
        )
    }
    query = (q or "").strip()[:100]
    searched = [a for a in all_artists if alphabetical(query) in alphabetical(a.name)]

    def matches(a: Artist, nf: str | None, art: str | None, fresh: bool) -> bool:
        if inventory is not None and inventory.walk_errors and (nf or art):
            return False  # Unknown metadata must not be called missing.
        return (
            (
                not nf
                or (nf == "with" and bool(a.nfo_paths))
                or (nf == "missing" and not a.nfo_paths)
                or (nf == "conflict" and len(a.nfo_paths) > 1)
            )
            and (not art or (a.artwork_path is not None) == (art == "with"))
            and (not fresh or a.key in recent)
        )

    rows = [a for a in searched if matches(a, nfo, artwork, new)]
    pages = max(1, (len(rows) + 29) // 30)
    page = min(max(1, page), pages)

    def location(
        number: int = 1, *, nf: str | None = nfo, art: str | None = artwork, fresh: bool = new
    ) -> str:
        return index_url(number, query, nfo=nf, artwork=art, new=fresh)

    facets = []
    for key, title, selected, choices in (
        (
            "nfo",
            "NFO",
            nfo,
            (("with", "With NFO"), ("missing", "Without NFO"), ("conflict", "Conflicting NFOs")),
        ),
        ("artwork", "Artwork", artwork, (("with", "With artwork"), ("missing", "Missing artwork"))),
    ):
        facets.append(
            {
                "key": key,
                "title": title,
                "selected": selected,
                "clear_url": location(nf=None) if key == "nfo" else location(art=None),
                "choices": [
                    {
                        "value": value,
                        "label": label,
                        "active": selected == value,
                        "count": sum(
                            matches(
                                a,
                                value if key == "nfo" else nfo,
                                artwork if key == "nfo" else value,
                                new,
                            )
                            for a in searched
                        ),
                        "url": location(nf=value) if key == "nfo" else location(art=value),
                    }
                    for value, label in choices
                ],
            }
        )
    return {
        "rows": rows[(page - 1) * 30 : page * 30],
        "q": query,
        "page": page,
        "pages": pages,
        "total": len(rows),
        "count": len(all_artists),
        "nfo": nfo,
        "artwork": artwork,
        "new": new,
        "facets": facets,
        "new_count": sum(matches(a, nfo, artwork, True) for a in searched),
        "new_url": location(fresh=not new),
        "clear_url": index_url(1, query),
        "filtered": bool(nfo or artwork or new),
        "new_ids": recent,
        "detail_urls": {
            a.key: detail_url(
                a.key,
                artist_page=page,
                artist_q=query,
                artist_nfo=nfo,
                artist_artwork=artwork,
                artist_new=new,
            )
            for a in rows[(page - 1) * 30 : page * 30]
        },
        "image_urls": {
            a.key: inventory.image_url(a.mbid) for a in rows[(page - 1) * 30 : page * 30]
        }
        if inventory is not None
        else {},
        "missing_id_albums": tuple(
            sorted(
                (
                    a
                    for a in albums
                    if not a.artist_credits
                    or any(not mbid(c.mbid or "") or c.missing_id_count for c in a.artist_credits)
                ),
                key=lambda a: (alphabetical(a.title), a.id),
            )
        ),
        "url": location(page),
        "previous": location(page - 1) if page > 1 else None,
        "next": location(page + 1) if page < pages else None,
    }
