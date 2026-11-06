"""The images the Cover Art Archive's stored listings offer an album (#659, #663).

Every function here reads what `caa_cache`, `mb_cache` and `cover_art` already
hold, and none of them fetches: they answer what is in hand, for the album page
drawing its picker and for a tagging writing what the page offered.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import NamedTuple

from . import artwork, caa_cache, cover_art, derived, formats, images, mb_cache, mb_lookup
from .models import BorrowedArtwork, Release

log = logging.getLogger(__name__)


class Listed(NamedTuple):
    """One image a listing names, and which listing: `artwork.Candidate`'s
    origins, and for another release, what tells it apart."""

    origin: str
    listing: cover_art.Candidate
    #: The MBID of the release or release group whose listing it is.
    source: str
    release: str | None = None


def group_of(mbid: str) -> str | None:
    """The release's group, from the STORED release payload — a local read
    with no MusicBrainz request in it (#434)."""
    stored = mb_cache.stored_release(mbid)
    group = (stored.get("release-group") or {}).get("id") if stored else None
    return group if isinstance(group, str) else None


def other_releases(mbid: str, group: str) -> list[Release]:
    """The group's other releases, as its last browse listed them, leaving out
    any MusicBrainz says has no artwork in the archive: listing one of those
    would spend a request to learn nothing."""
    stored = mb_cache.stored_release_group_editions(group)
    return [
        r
        for r in (stored[0] if stored else [])
        if r.get("id") != mbid and (r.get("cover-art-archive") or {}).get("artwork") != "false"
    ]


def front_listed_since_answer(mbid: str) -> bool:
    """Whether a release in the group has been listed with a front since the
    archive check answered that neither this release nor its group has one
    (#974).

    MusicBrainz gives a group the front of one of its releases, so that answer
    is out of date, and the listing is the newer of the two. Left to its TTL,
    the picker would offer the image while the plan beside it, read from the
    answer, had nothing to add. Asking again settles it either way: the new
    answer postdates the listing, so each listing prompts one check at most,
    and a check that failed is due again.
    """
    known = caa_cache.stored(mbid)
    group = group_of(mbid)
    if known is None or known.has_art or group is None:
        return False
    for release in other_releases(mbid, group):
        listed = caa_cache.listed_at("release", str(release["id"]))
        if (
            listed is not None
            and listed > known.fetched_at
            and any(c.front for c in caa_cache.stored_listing("release", str(release["id"])) or ())
        ):
            return True
    return False


def _release_label(release: Release) -> str:
    """What tells one release of an album from another: the same facts the
    release pickers show, minus the title they share."""
    s = mb_lookup.release_summary(release)
    return " · ".join(
        str(v) for v in (s["disambiguation"], s["date"], s["country"], s["media"]) if v
    )


def listings(mbid: str | None) -> list[Listed]:
    """Every image the archive lists for this release, then its group, then
    any other release in the group someone has stepped to (#659) — from what
    is stored, never the network.

    Another release's listing is kept only while it is fresh: nothing else
    refreshes it, so a stale one drops out and the next step lists it again.
    The group's stored browse only says which listings are its releases'; it
    decides nothing about what to ask for next (see
    `web.main._list_another_release`).
    """
    if mbid is None:
        return []
    listed = [
        Listed(artwork.Candidate.RELEASE, c, mbid)
        for c in caa_cache.stored_listing("release", mbid) or []
    ]
    group = group_of(mbid)
    if group is None:
        return listed
    listed += [
        Listed(artwork.Candidate.RELEASE_GROUP, c, group)
        for c in caa_cache.stored_listing("release-group", group) or []
    ]
    for release in other_releases(mbid, group):
        release_id = str(release["id"])
        stored = caa_cache.stored_listing("release", release_id, fresh=True)
        label = _release_label(release)
        listed += [
            Listed(artwork.Candidate.OTHER_RELEASE, c, release_id, label) for c in stored or []
        ]
    return listed


def picked_originals(mbid: str | None, choices: artwork.Choices) -> dict[str, cover_art.Front]:
    """The originals of the archive images `choices` name by id, as
    `in_hand` finds them — what an apply writes them from (#659). One not in
    hand is simply absent, and the plan rebuilt without it fails the page's
    fingerprint.

    By id alone for one no listing names any more: another release's image
    stays chosen after its listing lapses, and was fetched under its id when
    it was chosen. A lookup in the cache, never a fetch."""
    found: dict[str, cover_art.Front] = {}
    ids = {c for c in choices.values() if c.isdigit()}
    if not ids:
        return found
    listed = {x.listing.image_id: x.listing for x in listings(mbid)}
    for choice in ids:
        listing = listed.get(choice)
        front = (
            in_hand(mbid, listing)
            if listing is not None
            else cover_art.cached_candidate_image(choice)
        )
        if front is not None:
            found[choice] = front
    return found


def in_hand(mbid: str | None, listing: cover_art.Candidate) -> cover_art.Front | None:
    """A listed image's original, if it is already here (#659) — never the
    network. Here once it was chosen, which fetched it under its id; or when
    it is the release's own front, which the archive check downloads itself
    and keeps under the release's id instead."""
    if (front := cover_art.cached_candidate_image(listing.image_id)) is not None:
        return front
    caa = caa_cache.stored(mbid) if mbid else None
    if mbid is not None and caa is not None and caa.image_url == listing.image_url:
        return cover_art.cached_front(mbid)
    return None


def _has_own_front(mbid: str) -> bool:
    """Whether the release has a front of its own on the archive, as far as
    what is stored says (#663) — its own listing names one, or the archive
    check's answer came from the release rather than its group."""
    if any(c.front for c in caa_cache.stored_listing("release", mbid) or []):
        return True
    caa = caa_cache.stored(mbid)
    return caa is not None and caa.image_url is not None and not caa.from_release_group


def _borrowable(mbid: str) -> dict[str, BorrowedArtwork]:
    """The archive images in hand that would be stand-ins if written now (#663):
    the release group's and other releases', by digest, as their record would
    read. The release's own images are not, and neither is anything not in hand
    — an image is written only once its original is.
    """
    found: dict[str, BorrowedArtwork] = {}
    for listed in listings(mbid):
        if listed.origin == artwork.Candidate.RELEASE:
            continue
        original = in_hand(mbid, listed.listing)
        if original is None:
            continue
        found.setdefault(
            images.digest(original.data),
            BorrowedArtwork(
                digest=images.digest(original.data),
                source="release-group"
                if listed.origin == artwork.Candidate.RELEASE_GROUP
                else "release",
                source_mbid=listed.source,
                release=mbid,
                image_id=listed.listing.image_id,
            ),
        )
    # The archive check's own answer when it came from the group: the fallback
    # a tagging has written since #434, measured before any listing was kept.
    caa = caa_cache.stored(mbid)
    group = group_of(mbid)
    if (
        caa is not None
        and caa.from_release_group
        and group is not None
        and (front := cover_art.cached_front(mbid)) is not None
    ):
        digest = images.digest(front.data)
        found.setdefault(digest, BorrowedArtwork(digest, "release-group", group, mbid))
    return found


def stand_ins_after(
    album_path: Path,
    files: Sequence[Path],
    mbid: str,
    recorded: Sequence[BorrowedArtwork],
    written: Sequence[str],
) -> tuple[BorrowedArtwork, ...]:
    """The album's stand-in covers once an action has written `written` (#663).

    Kept: what was recorded for this release that the album still carries —
    so a rematch lapses the record, and replacing a stand-in (with the
    release's own front, or anything else) clears it. Added: a written image
    borrowed from another listing, but only while the release has no front of
    its own; one chosen over the release's own front is a choice, not a
    stand-in, and is never offered back.

    Reads the album's images only when something was written: an action that
    wrote none changed nothing they carry.

    A written image may be a squished copy of the one borrowed (#888). It's
    found by its original, and recorded as the copy, as the album carries it.
    """
    current = tuple(b for b in recorded if b.release == mbid)
    if not written:
        return current
    try:
        carried = {
            images.digest(art[0]) for f in files if (art := formats.read_cover(f)) is not None
        }
        if (cover := cover_art.cached_cover(album_path)) is not None:
            carried.add(images.digest(cover.read_bytes()))
    except formats.READ_ERRORS:
        # Could not tell what the album carries now, so nothing recorded is
        # dropped on the strength of it; it lapses on the next write instead.
        log.warning("could not read %s's artwork after writing it", album_path, exc_info=True)
        kept = list(current)
    else:
        kept = [b for b in current if b.digest in carried]
    if not _has_own_front(mbid):
        borrowable = _borrowable(mbid)
        originals = derived.originals()
        held = {b.digest for b in kept}
        for d in written:
            found = borrowable.get(originals.get(d, d))
            if found is not None and d not in held:
                kept.append(replace(found, digest=d))
    return tuple(kept)
