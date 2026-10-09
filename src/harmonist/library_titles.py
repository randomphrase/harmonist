"""Library membership used by conditional title spelling (#722).

Scans supply identities from tags; sidecar writes update confirmed matches;
MusicBrainz reads supply the latest group of each release. Nothing is persisted
and nothing here fetches. Each tagging takes an immutable snapshot so its files
all receive the same spelling even if the library changes during the write.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path

from .models import Album, Release, Sidecar
from .transforms import TaggingChoices

Membership = frozenset[tuple[str, str]]
ChangeReader = Callable[[Membership, Membership], None]
_lock = threading.Lock()
_paths: dict[Path, str] = {}
_published_paths: dict[Path, str] = {}
_tag_groups: dict[str, str] = {}
_observed_groups: dict[str, str | None] = {}
_on_change: ChangeReader | None = None
_published: Membership = frozenset()


def configure(on_change: ChangeReader | None) -> None:
    """Start a library's lifetime; callbacks run outside the index lock."""
    global _on_change, _published
    with _lock:
        _paths.clear()
        _published_paths.clear()
        _tag_groups.clear()
        _observed_groups.clear()
        _published = frozenset()
        _on_change = on_change


def _membership(
    excluding: Iterable[Path] = (), *, paths: dict[Path, str] | None = None
) -> Membership:
    excluded = set(excluding)
    return frozenset(
        (mbid, group)
        for path, mbid in (_paths if paths is None else paths).items()
        if path not in excluded
        if (group := _observed_groups.get(mbid, _tag_groups.get(mbid)))
    )


def _multiple(members: Membership) -> frozenset[str]:
    groups: dict[str, set[str]] = {}
    for mbid, group in members:
        groups.setdefault(group, set()).add(mbid)
    return frozenset(group for group, releases in groups.items() if len(releases) > 1)


def _changed(before: Membership, after: Membership) -> None:
    # A singleton appearing cannot change any existing title. In particular,
    # loading a cold cache must not launch one recheck per release.
    callback = _on_change
    affected = {group for _, group in before ^ after}
    if callback is not None and affected & (_multiple(before) | _multiple(after)):
        callback(before, after)


def reset_from(albums: Iterable[Album]) -> None:
    """Replace membership after a complete or partial scan publishes its albums."""
    global _published
    with _lock:
        before = _published
        _paths.clear()
        groups: dict[str, set[str]] = {}
        for album in albums:
            tagged = album.tagged_release_group
            if tagged:
                groups.setdefault(tagged[0], set()).add(tagged[1])
            mbid = album.sidecar.mb_release_id if album.sidecar else tagged[0] if tagged else None
            if mbid:
                for path in album.folders:
                    _paths[path] = mbid
        _tag_groups.clear()
        _tag_groups.update((mbid, next(iter(ids))) for mbid, ids in groups.items() if len(ids) == 1)
        after = _membership()
        _published_paths.clear()
        _published_paths.update(_paths)
        _published = after
    _changed(before, after)


def upsert(path: Path, sidecar: Sidecar | None) -> None:
    """Follow confirmed identity changes at the existing sidecar index hook."""
    with _lock:
        if sidecar is not None and sidecar.mb_release_id:
            _paths[path] = sidecar.mb_release_id
        else:
            _paths.pop(path, None)
    # Writes need the new identity immediately. Rechecks wait for publication
    # of the next scan so they also reach the newly added/rematched Album.


def observe(release: Release) -> None:
    """Prefer MusicBrainz's current group to a group's older spelling in tags."""
    global _published
    mbid = str(release["id"])
    group = (release.get("release-group") or {}).get("id")
    with _lock:
        if mbid in _observed_groups and _observed_groups[mbid] == group:
            return
        before = _published
        _observed_groups[mbid] = str(group) if group else None
        # Only recheck albums the scanner has already published. A sidecar
        # write can precede that scan; reset_from will recheck its new album.
        after = _membership(paths=_published_paths)
        _published = after
    _changed(before, after)


def choices(settings: TaggingChoices, *, excluding: Iterable[Path] = ()) -> TaggingChoices:
    """Freeze membership for a plan; exclude the album being (re)matched.

    The release in the plan supplies its own membership. Excluding only this
    album's paths keeps other copies visible without counting its old match as
    another release alongside the new one.
    """
    with _lock:
        return replace(settings, library_releases=_membership(excluding))
