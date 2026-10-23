"""Read-only Kodi/Jellyfin artist enrichment, associated by confirmed artist ID.

An optional Artist Information Folder is independent of the audio layout. File
names and artist names are discovery hints, never proof of artist identity.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from stat import S_ISDIR, S_ISREG
from xml.etree import ElementTree as ET

from .artists import Artist, mbid

log = logging.getLogger(__name__)
_MAX_BYTES = 1024 * 1024
_IMAGES = {".jpg", ".jpeg", ".png", ".webp"}


class NfoError(ValueError):
    """Metadata exists but could not be read safely; distinct from absence."""


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


@dataclass(frozen=True)
class Result:
    profile: Profile | None = None
    errors: tuple[str, ...] = ()
    candidates: tuple[Path, ...] = ()


def selection_key(path: Path) -> str:
    return hashlib.sha256(str(path).encode()).hexdigest()[:24]


def read(path: Path) -> Profile:
    """Cache successful parses only, invalidated by external edits on every view."""
    try:
        info = path.stat()
        if not S_ISREG(info.st_mode):
            raise NfoError(f"{path}: not a regular file")
        return _read(path, info.st_mtime_ns, info.st_size)
    except OSError as exc:
        raise NfoError(f"{path}: {exc.strerror}") from exc


@lru_cache(maxsize=1024)
def _read(path: Path, modified_ns: int, size: int) -> Profile:
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
    )


def local_image(profile: Profile) -> Path | None:
    """Only local raster artwork inside the NFO's directory is served."""
    base = profile.path.parent.resolve()
    refs = (profile.image_ref, "folder.jpg", "folder.png", "artist.jpg", "artist.png")
    for reference in refs:
        if not reference or ":" in reference or reference.startswith(("/", "\\")):
            continue
        path = (base / reference).resolve()
        if not path.is_relative_to(base) or path.suffix.lower() not in _IMAGES:
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


def _candidate_paths(artist: Artist, music_dir: Path, info_dir: Path | None) -> set[Path]:
    root = music_dir.resolve()
    directories: set[Path] = set()
    for album in artist.albums:
        for folder in album.folders:
            current = folder.resolve()
            while current.is_relative_to(root):
                directories.add(current)
                if current == root:
                    break
                current = current.parent
    candidates = {d / "artist.nfo" for d in directories if _present(d / "artist.nfo")}
    if info_dir is not None:
        info_root = info_dir.resolve()
        # One folder per artist, as in Kodi's Artist Information Folder. Listing
        # directly makes missing/unreadable configured roots a visible error.
        for child in info_root.iterdir():
            candidate = child / "artist.nfo"
            if S_ISDIR(child.stat().st_mode) and _present(candidate):
                candidates.add(candidate)
        if _present(info_root / "artist.nfo"):
            candidates.add(info_root / "artist.nfo")
    allowed = (root,) if info_dir is None else (root, info_dir.resolve())
    return {p for p in candidates if any(p.resolve().is_relative_to(r) for r in allowed)}


def for_artist(
    artist: Artist,
    music_dir: Path,
    info_dir: Path | None = None,
    selection: str | None = None,
    *,
    refresh: bool = False,
) -> Result:
    """No network or writes; ambiguous NFOs require an explicit page selection."""
    if artist.mbid is None:
        return Result()
    if refresh:
        _read.cache_clear()
    try:
        candidates = _candidate_paths(artist, music_dir, info_dir)
    except OSError as exc:
        log.exception("Could not inspect artist information folders")
        return Result(errors=(f"Could not inspect artist information folders: {exc}",))
    errors: list[str] = []
    matches: list[Profile] = []
    for path in sorted(candidates):
        try:
            profile = read(path)
        except NfoError as exc:
            log.exception("Could not read artist NFO %s", path)
            errors.append(str(exc))
            continue
        if profile.mbid == artist.mbid:
            matches.append(profile)
    paths = tuple(p.path for p in matches)
    selected = [p for p in matches if selection_key(p.path) == selection]
    if len(selected) == 1:
        return Result(selected[0], tuple(errors), paths)
    if len(matches) > 1:
        errors.append("Multiple artist.nfo files identify this artist. Choose a source.")
        return Result(errors=tuple(errors), candidates=paths)
    if selection:
        errors.append("The selected artist.nfo is no longer available for this artist.")
        return Result(errors=tuple(errors), candidates=paths)
    return Result(matches[0] if matches else None, tuple(errors), paths)
