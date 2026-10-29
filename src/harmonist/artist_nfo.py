"""Read-only Kodi/Jellyfin artist enrichment, associated by confirmed artist ID.

The scanner discovers files throughout the library. Names and placement never
establish identity; only the artist ID inside the file can associate metadata.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from functools import cached_property, lru_cache
from pathlib import Path
from stat import S_ISREG
from xml.etree import ElementTree as ET

from .artists import mbid
from .models import Album

log = logging.getLogger(__name__)
_MAX_BYTES = 1024 * 1024
_IMAGES = {".jpg", ".jpeg", ".png", ".webp"}
_ADDITIONAL = {"Disambiguation", "Years active", "Style", "Mood", "Instruments"}


class NfoError(ValueError):
    """Metadata exists but could not be read safely; distinct from absence."""


@dataclass(frozen=True)
class DiscographyAlbum:
    title: str
    year: str
    release_group_id: str | None


@dataclass(frozen=True)
class Profile:
    path: Path
    mbid: str | None
    name: str
    sort_name: str
    biography: str
    facts: tuple[tuple[str, str], ...]
    image_ref: str
    modified_ns: int
    # Resolved by discovery, separately from the cached XML parse. A picture
    # can change while the NFO remains byte-for-byte identical.
    image_path: Path | None = None
    image_stamp: tuple[int, ...] = ()
    genres: tuple[str, ...] = ()
    discography: tuple[DiscographyAlbum, ...] = ()

    @property
    def main_facts(self) -> tuple[tuple[str, str], ...]:
        return tuple((k, v) for k, v in self.facts if k not in _ADDITIONAL | {"Genre"})

    @property
    def additional_facts(self) -> tuple[tuple[str, str], ...]:
        return tuple((k, v) for k, v in self.facts if k in _ADDITIONAL)


@dataclass(frozen=True)
class Result:
    profile: Profile | None = None
    errors: tuple[str, ...] = ()
    candidates: tuple[Path, ...] = ()


def read(path: Path) -> Profile:
    """Cache successful parses; file identity and timestamps detect external edits."""
    try:
        info = path.stat()
        if not S_ISREG(info.st_mode):
            raise NfoError(f"{path}: not a regular file")
        return _read(path, info.st_mtime_ns, info.st_size, info.st_ctime_ns, info.st_ino)
    except OSError as exc:
        raise NfoError(f"{path}: {exc.strerror}") from exc


@lru_cache(maxsize=1024)
def _read(path: Path, modified_ns: int, size: int, _ctime_ns: int, _inode: int) -> Profile:
    if size > _MAX_BYTES:
        raise NfoError(f"{path}: artist.nfo exceeds 1 MiB")
    with path.open("rb") as stream:
        data = stream.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise NfoError(f"{path}: artist.nfo exceeds 1 MiB")
    # Also cover UTF-16/32 XML: ASCII declarations are interleaved with NULs.
    declarations = data.replace(b"\0", b"").upper()
    if b"<!DOCTYPE" in declarations or b"<!ENTITY" in declarations:
        raise NfoError(f"{path}: DTD and entity declarations are not supported")
    try:
        root = ET.fromstring(data)
    except (ET.ParseError, LookupError, ValueError) as exc:
        raise NfoError(f"{path}: invalid artist XML ({exc})") from exc
    if root.tag != "artist":
        raise NfoError(f"{path}: expected an <artist> root")

    def values(*tags: str) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                text
                for tag in tags
                for node in root.findall(tag)
                if (text := "".join(node.itertext()).strip())
            )
        )

    def text(*tags: str) -> str:
        return " · ".join(values(*tags))

    ids = values("musicBrainzArtistID", "musicbrainzartistid")
    identified = {mbid(value) for value in ids}
    if ids and (None in identified or len(identified) != 1):
        raise NfoError(f"{path}: invalid or conflicting MusicBrainz artist IDs")
    facts = tuple(
        (label, value)
        for label, tags in (
            ("Disambiguation", ("disambiguation",)),
            ("Type", ("type",)),
            ("Gender", ("gender",)),
            ("Born", ("born",)),
            ("Formed", ("formed",)),
            ("Died", ("died",)),
            ("Disbanded", ("disbanded",)),
            ("Years active", ("yearsactive",)),
            ("Genre", ("genre",)),
            ("Style", ("style",)),
            ("Mood", ("mood",)),
            ("Instruments", ("instruments",)),
        )
        if (value := text(*tags))
    )
    return Profile(
        path,
        next(iter(identified)) if identified else None,
        text("name"),
        text("sortname", "sortName"),
        text("biography"),
        facts,
        next(iter(values("thumb")), ""),
        modified_ns,
        genres=values("genre"),
        discography=tuple(
            dict.fromkeys(
                DiscographyAlbum(
                    title,
                    album.findtext("year", "").strip(),
                    mbid(album.findtext("musicbrainzreleasegroupid", "")),
                )
                for album in root.findall("album")
                if (title := album.findtext("title", "").strip())
            )
        ),
    )


def local_image(
    profile: Profile, *, root: Path | None = None, artist_dirs: frozenset[Path] = frozenset()
) -> Path | None:
    """Prefer declared local artwork; generic names need artist-folder evidence."""
    base = profile.path.parent.resolve()
    allowed = root.resolve() if root is not None else base
    explicit = ("artist.jpg", "artist.png", "artist.jpeg", "artist.webp")
    generic = (
        (
            "thumb.jpg",
            "thumb.png",
            "thumb.jpeg",
            "thumb.webp",
            "folder.jpg",
            "folder.png",
            "folder.jpeg",
            "folder.webp",
        )
        if base in artist_dirs
        else ()
    )
    refs: tuple[str, ...] = (profile.image_ref, *explicit, *generic)
    for reference in refs:
        if not reference or ":" in reference or reference.startswith("\\"):
            continue
        path = (base / reference).resolve()
        if not path.is_relative_to(allowed) or path.suffix.lower() not in _IMAGES:
            continue
        if _present(path) and S_ISREG(path.stat().st_mode):
            return path
    return None


def _present(path: Path) -> bool:
    """Absence is normal; permission and filesystem failures must propagate."""
    try:
        path.stat()
    except FileNotFoundError:
        # No metadata/art is a legitimate result, unlike an unreadable file.
        return False
    return True


@dataclass(frozen=True)
class Inventory:
    """One completed scan's local NFO evidence, including unreadable files."""

    paths: tuple[Path, ...] = ()
    profiles: tuple[Profile, ...] = ()
    errors: tuple[str, ...] = ()
    walk_errors: tuple[str, ...] = ()
    album_dirs: frozenset[Path] = frozenset()

    def image_for(self, identity: str | None) -> Path | None:
        profile = self.for_artist(identity).profile
        return profile.image_path if profile else None

    def image_url(self, identity: str | None) -> str | None:
        profile = self.for_artist(identity).profile
        if profile is None or profile.image_path is None:
            return None
        # A browser can reuse a decoded image within one document without an
        # HTTP request at all. Change src when the scan observed a new picture;
        # keep it stable otherwise, with normal ETag revalidation on the route.
        version = hashlib.sha256(
            repr((profile.image_path, profile.image_stamp)).encode()
        ).hexdigest()[:16]
        return f"/artist/{profile.mbid}/image?v={version}"

    @cached_property
    def by_mbid(self) -> dict[str, tuple[Profile, ...]]:
        groups: dict[str, list[Profile]] = {}
        for profile in self.profiles:
            if profile.mbid:
                groups.setdefault(profile.mbid, []).append(profile)
        return {key: tuple(values) for key, values in groups.items()}

    @cached_property
    def conflicts(self) -> dict[str, tuple[Profile, ...]]:
        return {key: profiles for key, profiles in self.by_mbid.items() if len(profiles) > 1}

    def for_artist(self, identity: str | None) -> Result:
        if self.walk_errors:
            return Result(errors=("NFO inventory unavailable.", *self.walk_errors))
        profiles = self.by_mbid.get(identity or "", ())
        paths = tuple(p.path for p in profiles)
        if len(profiles) > 1:
            return Result(
                errors=("Conflicting artist.nfo files have the same artist ID.",), candidates=paths
            )
        return Result(profiles[0] if profiles else None, candidates=paths)

    def orphans(self, albums: Sequence[Album]) -> tuple[Profile, ...]:
        credited = {c.mbid for a in albums for c in a.artist_credits if c.mbid}
        return tuple(p for p in self.profiles if p.mbid is None or p.mbid not in credited)


@dataclass
class Discovery:
    """NFO names collected by the album walk, including non-album directories."""

    root: Path
    paths: list[Path] = field(default_factory=list)
    walk_errors: list[str] = field(default_factory=list)
    album_dirs: set[Path] = field(default_factory=set)

    def on_error(self, error: OSError) -> None:
        log.error("Could not discover artist NFOs: %s", error, exc_info=error)
        self.walk_errors.append(str(error))

    def finish(self, albums: Sequence[Album] = ()) -> Inventory:
        # An incomplete walk cannot establish absence, uniqueness or orphanhood.
        if self.walk_errors:
            return Inventory(walk_errors=tuple(self.walk_errors))
        profiles: list[Profile] = []
        errors: list[str] = []
        paths: list[Path] = []
        album_dirs = frozenset(self.album_dirs)
        root = self.root.resolve()
        blocked = set(album_dirs)
        directory_credits: dict[Path, set[str]] = {}
        for album in albums:
            credits = {c.mbid for c in album.artist_credits if c.mbid}
            for folder in album.folders:
                if not folder.is_relative_to(self.root):
                    continue
                # The walk never follows nested directory symlinks. Rebase its
                # known paths onto the resolved root without a filesystem lookup
                # per album during every settled picture/NFO event on a NAS.
                folder = root / folder.relative_to(self.root)
                blocked.add(folder)
                # A directory containing a tagged disc of a larger release can
                # be the album container even without audio or album.nfo of its own.
                if album.disc_total and album.disc_total > 1 and album.absent_media:
                    blocked.add(folder.parent)
                parent = folder.parent
                while parent.is_relative_to(root):
                    if parent in directory_credits:
                        directory_credits[parent].intersection_update(credits)
                    else:
                        directory_credits[parent] = set(credits)
                    if parent == root:
                        break
                    parent = parent.parent
        for path in sorted(set(self.paths)):
            try:
                if not path.resolve().is_relative_to(root):
                    raise NfoError(f"{path}: NFO points outside the music library")
                # A deletion between discovery and reading is normal. Other I/O
                # errors remain visible and must not masquerade as absence.
                if not _present(path):
                    continue
                paths.append(path)
                profile = read(path)
                profiles.append(profile)
                base = path.parent.resolve()
                artist_dirs = (
                    frozenset({base})
                    if base not in blocked and profile.mbid in directory_credits.get(base, set())
                    else frozenset()
                )
                image = local_image(profile, root=root, artist_dirs=artist_dirs)
                if image is not None:
                    info = image.stat()
                    if info.st_size > 20 * 1024 * 1024:
                        raise NfoError(f"{image}: artist artwork exceeds 20 MiB")
                    profiles[-1] = replace(
                        profile,
                        image_path=image,
                        image_stamp=(info.st_mtime_ns, info.st_size, info.st_ctime_ns, info.st_ino),
                    )
            except (NfoError, OSError) as exc:
                log.exception("Could not read artist NFO %s", path)
                if path not in paths:
                    paths.append(path)
                errors.append(str(exc) if isinstance(exc, NfoError) else f"{path}: {exc}")
        return Inventory(
            tuple(paths),
            tuple(profiles),
            tuple(errors),
            album_dirs=album_dirs,
        )


def scan(root: Path) -> Inventory:
    """Synchronous fallback when no background scanner is engaged (TestClient)."""
    # The scanner imports the NFO collector, so this fallback imports it only
    # at call time. Production supplies the already-read albums to finish().
    from . import scanner

    discovery = Discovery(root)
    entries = scanner.scan_dirs(root, nfos=discovery)
    return discovery.finish([entry.album for entry in entries])
