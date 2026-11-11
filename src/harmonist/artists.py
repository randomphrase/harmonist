"""Artist browsing derived from the scanner snapshot, never a second catalogue.

Consistent, aligned album credits establish identity. Missing IDs can inherit
that identity only within one identified release and with agreeing names.
Spelling alone cannot create an artist page or bind an NFO. An artist.nfo no
album credits is an artist all the same (#936): under its artist ID, or under
its path while it has none.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from .formats.types import ScanFields
from .models import Album, AlbumArtist, AlbumState

if TYPE_CHECKING:
    from .artist_nfo import Inventory, Profile

_MBID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE)
# `id_registry`'s path hashes, the handle of an NFO that names no artist yet.
_PATH_KEY = re.compile(r"[0-9a-f]{32}")
VIEWS = ("list", "cards")
NOT_IDENTIFIED = "Not identified"

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


def page_key(value: str) -> str | None:
    """An artist page's key from a URL: an artist ID, or an NFO's path hash."""
    return mbid(value) or (value if _PATH_KEY.fullmatch(value) else None)


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
    if aligned and not all(f.album_artist_ids for f in fields):
        # Absence is not disagreement. Scope the available artist IDs to one
        # release carried by EVERY file; never attach a name-only album to an
        # artist identified somewhere else in the library.
        releases = {mbid(f.album_id or "") for f in fields}
        aligned = len(releases) == 1 and None not in releases
    sorts = {f.album_artist_sort for f in fields}
    sort = next(iter(sorts)) if len(sorts) == 1 and len(names) == 1 else None
    return tuple(
        AlbumArtist(name, ids[i] if aligned else None, sort) for i, name in enumerate(names)
    )


def alphabetical(value: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", value.casefold()) if not unicodedata.combining(c)
    )


def credit_parts(album: Album) -> tuple[tuple[str, str | None], ...]:
    """Link exact, unique credit names without reconstructing ALBUMARTIST.

    Separate names can be standardised independently of the displayed credit.
    Missing, repeated or out-of-order matches leave the original text intact;
    a substring inside another word cannot identify the credited artist.
    """
    plain = ((album.artist, None),)
    parts: list[tuple[str, str | None]] = []
    offset = 0
    for credit in album.artist_credits:
        if not credit.name:
            return plain
        matches = list(re.finditer(r"(?<!\w)" + re.escape(credit.name) + r"(?!\w)", album.artist))
        if len(matches) != 1 or matches[0].start() < offset:
            return plain
        match = matches[0]
        if match.start() > offset:
            parts.append((album.artist[offset : match.start()], None))
        parts.append((match.group(), mbid(credit.mbid or "")))
        offset = match.end()
    if not parts:
        return plain
    if offset < len(album.artist):
        parts.append((album.artist[offset:], None))
    return tuple(parts)


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


def build_index(albums: Sequence[Album], inventory: Inventory | None = None) -> list[Artist]:
    """Share identified collaborations without coalescing homonymous artists.

    With an inventory, its NFOs that no album credits are artists too, with no
    albums. A credited artist keeps the tagged name; one known only from NFOs
    takes the first name they give, or the folder's when none gives one. That
    name is only shown: the key is the ID, or the path of a file without one.
    """
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
    if inventory is not None:
        for key, profiles in inventory.by_key.items():
            if key in credits or key in SPECIAL_ARTIST_IDS:
                continue
            name = next((p.name for p in profiles if p.name), profiles[0].path.parent.name)
            sort_name = next((p.sort_name for p in profiles if p.sort_name), None)
            result.append(Artist(key, name, profiles[0].mbid, sort_name, ()))
    return sorted(result, key=lambda a: (alphabetical(a.name), a.key))


@dataclass(frozen=True)
class Hint:
    """An artist the albums beneath an NFO's folder credit, and how many do.
    Named as MusicBrainz names it, with its disambiguation, when one of those
    albums' releases is stored; by the tag's credit otherwise."""

    mbid: str
    name: str
    credited: int
    disambiguation: str = ""


@dataclass(frozen=True)
class Hints:
    """What an NFO's folder says about whom it describes (#996): the albums
    beneath it, and the artists they credit, most credited first."""

    albums: int
    artists: tuple[Hint, ...]

    @property
    def suggestion(self) -> Hint | None:
        """The one artist every album there credits. Two who share every album
        are a pair, and the folder can't say which of them the file is about."""
        every = [h for h in self.artists if h.credited == self.albums]
        return every[0] if len(every) == 1 else None

    @property
    def token(self) -> str:
        """Changes when what the folder offers does. A page keeps its finder
        across redraws, so that a search being read survives them; it keeps
        it only while this is the same, so an album reaching the Library shows
        up in the list."""
        return hashlib.sha256(repr((self.albums, self.artists)).encode()).hexdigest()[:12]


# What MusicBrainz last said about a release, from the store and never the
# network: `mb_cache.stored_release`.
StoredRelease = Callable[[str], Mapping[str, Any] | None]


def _musicbrainz_artist(release: Mapping[str, Any] | None, artist_id: str) -> Hint | None:
    """The artist's own name and disambiguation, from a release crediting it.
    A credit's `name` is how the release credits it, which can differ."""
    for credit in (release or {}).get("artist-credit") or ():
        artist = credit.get("artist") if isinstance(credit, Mapping) else None
        if isinstance(artist, Mapping) and mbid(str(artist.get("id") or "")) == artist_id:
            name = str(artist.get("name") or "")
            if not name:
                return None
            return Hint(artist_id, name, 0, str(artist.get("disambiguation") or ""))
    return None


def _hints(beneath: Sequence[Album], stored: StoredRelease) -> Hints:
    """The hints the albums beneath one NFO's folder give.

    Worked out afresh from the scan each time, never stored. Credits count
    only by their IDs, as in the index. An album crediting no one identified
    still counts, so that a suggestion is true of every album there. Naming an
    artist reads stored releases only, so drawing the hints asks MusicBrainz
    nothing; with none stored, the tag's credit names it."""
    tagged: dict[str, str] = {}
    credited: dict[str, int] = {}
    releases: dict[str, list[str]] = {}
    for album in beneath:
        keys: dict[str, str] = {}
        for credit in album.artist_credits:
            key = mbid(credit.mbid or "")
            if key is not None and key not in SPECIAL_ARTIST_IDS:
                keys.setdefault(key, credit.name)
        release = album.sidecar.mb_release_id if album.sidecar else None
        for key, name in keys.items():
            tagged.setdefault(key, name)
            credited[key] = credited.get(key, 0) + 1
            if release:
                releases.setdefault(key, []).append(release)
    read: dict[str, Mapping[str, Any] | None] = {}

    def named(key: str) -> Hint:
        for release in releases.get(key, ()):
            if release not in read:
                read[release] = stored(release)
            if found := _musicbrainz_artist(read[release], key):
                return replace(found, credited=credited[key])
        return Hint(key, tagged[key], credited[key])

    hints = [named(key) for key in credited]
    return Hints(
        len(beneath),
        tuple(sorted(hints, key=lambda h: (-h.credited, alphabetical(h.name), h.mbid))),
    )


@dataclass(frozen=True)
class Unidentified:
    """An NFO without an artist ID, as its Inbox card shows it (#996)."""

    profile: Profile
    hints: Hints

    @property
    def key(self) -> str:
        return self.profile.key

    @property
    def name(self) -> str:
        """The file's own name for its artist, or its folder's, as the index
        names an artist known only from an NFO."""
        return self.profile.name or self.profile.path.parent.name


def _beneath(albums: Sequence[Album], root: Path) -> dict[Path, list[Album]]:
    """Each folder in the library holding an album at any depth, with those
    albums, so that a file's hints are one lookup however many files there are:
    a Kodi library can have an NFO without our ID for every artist (#971).
    The top of the library is left out. It holds every album, so it says
    nothing about one artist."""
    folders: dict[Path, list[Album]] = {}
    for album in sorted(albums, key=lambda a: a.id):
        if not album.path.is_relative_to(root):
            continue
        for folder in (album.path, *album.path.parents):
            if folder == root:
                break
            folders.setdefault(folder, []).append(album)
    return folders


def folder_hints(
    profiles: Sequence[Profile], albums: Sequence[Album], root: Path, stored: StoredRelease
) -> dict[Path, Hints]:
    """What the albums beneath each NFO's folder suggest, by the NFO's path:
    the finder's **From contained albums**, wherever it is drawn."""
    beneath = _beneath(albums, root)
    return {p.path: _hints(beneath.get(p.path.parent, ()), stored) for p in profiles}


def unidentified(
    inventory: Inventory, albums: Sequence[Album], root: Path, stored: StoredRelease
) -> list[Unidentified]:
    """The readable NFOs naming no artist yet, each with what the albums
    beneath its folder suggest."""
    profiles = [p for p in inventory.profiles if p.mbid is None]
    hints = folder_hints(profiles, albums, root, stored)
    return sorted(
        (Unidentified(p, hints[p.path]) for p in profiles),
        key=lambda u: (alphabetical(u.name), str(u.profile.path)),
    )


def badges(artist: Artist) -> tuple[str, ...]:
    """What the card and the page say about an artist's NFOs. A path-keyed
    artist has exactly one file, so the two never meet."""
    if artist.mbid is None:
        return (NOT_IDENTIFIED,)
    return ("NFO conflict",) if len(artist.nfo_paths) > 1 else ()


def library_count(albums: Sequence[Album], inventory: Inventory | None = None) -> int:
    """Global count for the consolidated status poll, without sorting or I/O."""
    return len(library_ids(albums, inventory))


def library_ids(albums: Sequence[Album], inventory: Inventory | None = None) -> frozenset[str]:
    """Eligible identities, without constructing or sorting artist groups."""
    credited = frozenset(
        identity
        for album in albums
        if album.state in {AlbumState.COMPLETE, AlbumState.INCOMPLETE}
        for credit in album.artist_credits
        if (identity := mbid(credit.mbid or "")) and identity not in SPECIAL_ARTIST_IDS
    )
    return (credited | nfo_keys(inventory)) if inventory is not None else credited


def nfo_keys(inventory: Inventory) -> frozenset[str]:
    """The artist pages an inventory's NFOs give, credited or not."""
    return frozenset(key for key in inventory.by_key if key not in SPECIAL_ARTIST_IDS)


def index_url(
    page: int = 1,
    q: str = "",
    *,
    nfo: str | None = None,
    artwork: str | None = None,
    albums: str | None = None,
    new: bool = False,
    view: str | None = None,
) -> str:
    params = {"artist_page": str(page), "artist_q": q}
    if nfo:
        params["nfo"] = nfo
    if artwork:
        params["artwork"] = artwork
    if albums:
        params["albums"] = albums
    if new:
        params["new"] = "true"
    if view is not None and view in VIEWS:
        params["view"] = view
    return "/artists?" + urlencode(params)


def detail_url(
    key: str,
    *,
    page: int = 1,
    artist_page: int = 1,
    artist_q: str = "",
    artist_nfo: str | None = None,
    artist_artwork: str | None = None,
    artist_albums: str | None = None,
    artist_new: bool = False,
    artist_view: str | None = None,
) -> str:
    params = {"page": str(page), "artist_page": str(artist_page), "artist_q": artist_q}
    if artist_nfo:
        params["artist_nfo"] = artist_nfo
    if artist_artwork:
        params["artist_artwork"] = artist_artwork
    if artist_albums:
        params["artist_albums"] = artist_albums
    if artist_new:
        params["artist_new"] = "true"
    if artist_view is not None and artist_view in VIEWS:
        params["artist_view"] = artist_view
    return f"/artist/{key}?{urlencode(params)}"


def page_vars(
    albums: Sequence[Album],
    page: int = 1,
    q: str | None = None,
    *,
    inventory: Inventory | None = None,
    nfo: str | None = None,
    artwork: str | None = None,
    album_filter: str | None = None,
    new: bool = False,
    now: datetime | None = None,
    view: str = "list",
) -> dict[str, object]:
    all_artists = build_index(albums, inventory)
    view = view if view in VIEWS else "list"
    if inventory is not None:
        all_artists = [
            replace(
                a,
                nfo_paths=inventory.for_artist(a.key).candidates,
                artwork_path=inventory.image_for(a.key),
            )
            for a in all_artists
        ]
    nfo = nfo if nfo in {"with", "missing", "conflict", "unidentified"} else None
    artwork = artwork if artwork in {"with", "missing"} else None
    album_filter = album_filter if album_filter in {"with", "none"} else None
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
    complete_inventory = inventory is not None and inventory.identities_complete
    complete_artwork = complete_inventory and inventory is not None and not inventory.image_errors
    # Preserve positive observations. An unavailable filter must not yield a
    # misleading empty result, including when it arrived via a bookmarked URL.
    if nfo in {"missing", "conflict"} and not complete_inventory:
        nfo = None
    if artwork == "missing" and not complete_artwork:
        artwork = None
    selected = {"nfo": nfo, "artwork": artwork, "albums": album_filter}
    query = (q or "").strip()[:100]
    searched = [a for a in all_artists if alphabetical(query) in alphabetical(a.name)]

    def matches(a: Artist, chosen: dict[str, str | None], fresh: bool) -> bool:
        nf, art, owned = chosen["nfo"], chosen["artwork"], chosen["albums"]
        return (
            (
                not nf
                or (nf == "with" and bool(a.nfo_paths))
                or (nf == "missing" and not a.nfo_paths)
                or (nf == "conflict" and len(a.nfo_paths) > 1)
                or (nf == "unidentified" and a.mbid is None)
            )
            and (not art or (a.artwork_path is not None) == (art == "with"))
            and (not owned or bool(a.albums) == (owned == "with"))
            and (not fresh or a.key in recent)
        )

    rows = [a for a in searched if matches(a, selected, new)]
    pages = max(1, (len(rows) + 29) // 30)
    page = min(max(1, page), pages)

    def location(
        number: int = 1, *, fresh: bool = new, change: tuple[str, str | None] | None = None
    ) -> str:
        """This page's URL, with one filter set to another value if asked."""
        chosen = dict(selected)
        if change is not None:
            chosen[change[0]] = change[1]
        return index_url(
            number,
            query,
            nfo=chosen["nfo"],
            artwork=chosen["artwork"],
            albums=chosen["albums"],
            new=fresh,
            view=view,
        )

    def unavailable(key: str, value: str) -> bool:
        return (key == "nfo" and value in {"missing", "conflict"} and not complete_inventory) or (
            key == "artwork" and value == "missing" and not complete_artwork
        )

    def partial(key: str, value: str) -> bool:
        # A count from an incomplete inventory is a lower bound. Albums come
        # from tags, so only the artists known from their NFOs alone are unsure.
        if key == "artwork":
            return not complete_artwork
        if key == "albums":
            return value == "none" and not complete_inventory
        return not complete_inventory

    facets = [
        {
            "key": key,
            "title": title,
            "clear_label": clear_label,
            "selected": selected[key],
            "clear_url": location(change=(key, None)),
            "choices": [
                {
                    "value": value,
                    "label": label,
                    "active": selected[key] == value,
                    "count": None
                    if unavailable(key, value)
                    else sum(matches(a, {**selected, key: value}, new) for a in searched),
                    "partial": partial(key, value),
                    "url": location(change=(key, value)),
                }
                for value, label in choices
            ],
        }
        for key, title, clear_label, choices in (
            (
                "nfo",
                "NFO",
                "Any NFO status",
                (
                    ("with", "With NFO"),
                    ("missing", "Without NFO"),
                    ("conflict", "Conflicting NFOs"),
                    ("unidentified", NOT_IDENTIFIED),
                ),
            ),
            (
                "artwork",
                "Artwork",
                "Any Artwork status",
                (("with", "With artwork"), ("missing", "Missing artwork")),
            ),
            (
                "albums",
                "Albums",
                "With or without albums",
                (("with", "With albums"), ("none", "No albums")),
            ),
        )
    ]
    return {
        "rows": rows[(page - 1) * 30 : page * 30],
        "q": query,
        "page": page,
        "pages": pages,
        "total": len(rows),
        "count": len(all_artists),
        "view": view,
        "view_urls": {
            choice: index_url(
                page,
                query,
                nfo=nfo,
                artwork=artwork,
                albums=album_filter,
                new=new,
                view=choice,
            )
            for choice in VIEWS
        },
        "badges": {artist.key: badges(artist) for artist in all_artists},
        "nfo": nfo,
        "artwork": artwork,
        "albums": album_filter,
        "new": new,
        "facets": facets,
        "new_count": sum(matches(a, selected, True) for a in searched),
        "new_url": location(fresh=not new),
        "clear_url": index_url(1, query, view=view),
        "filtered": bool(nfo or artwork or album_filter or new),
        "new_ids": recent,
        "detail_urls": {
            a.key: detail_url(
                a.key,
                artist_page=page,
                artist_q=query,
                artist_nfo=nfo,
                artist_artwork=artwork,
                artist_albums=album_filter,
                artist_new=new,
                artist_view=view,
            )
            for a in rows[(page - 1) * 30 : page * 30]
        },
        "image_urls": {a.key: inventory.image_url(a.key) for a in rows[(page - 1) * 30 : page * 30]}
        if inventory is not None
        else {},
        "url": location(page),
        "previous": location(page - 1) if page > 1 else None,
        "next": location(page + 1) if page < pages else None,
    }
