"""An album's artwork as a whole action: the plan read off the disk, and that
plan carried out, for every caller — with a page in front of it or without one
(#851).

The tagger writes images; this is everything around that write that makes it
the artwork of an *album*: which images the archive offers, which the album
borrowed (#663), whether a watch is satisfied, and what the Activity feed says
about it. The album page draws its Artwork section from `view` and applies it
with `apply`, so a caller with no page decides and writes exactly as it does.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from . import (
    activity,
    activity_store,
    album_files,
    artwork,
    artwork_watch,
    caa_cache,
    caa_listings,
    cover_art,
    derived,
    formats,
    images,
    timing,
)
from . import sidecar as sidecar_mod
from . import tagger as tagger_mod
from .models import Album

log = logging.getLogger(__name__)

# As long as the album page lets its own tag read take before it says so.
_SLOW_ALBUM_READ = timedelta(seconds=10)


def archive_image(mbid: str | None, *, release_only: bool = False) -> formats.EmbeddedArt | None:
    """The Cover Art Archive's image for this release, described, when one has
    been fetched (#276).

    Read from the local cache — the page never asks the archive on a render, and
    an album nobody has checked simply has no candidate here. Described rather
    than carried, like every other image on the page: the bytes go back to the
    browser through the image route, keyed by the digest measured here.
    """
    if mbid is None:
        return None
    front = cover_art.cached_front(mbid, release_only=release_only)
    return formats.EmbeddedArt.of(front.data, front.mime) if front else None


def measurement(mbid: str | None, listing: cover_art.Candidate) -> cover_art.Measure | None:
    """A listed image's measurement, if one is here (#659) — never the network.
    The picker's own, made the first time it showed the image; or for the
    release's front, the archive check's."""
    known = caa_cache.stored_measure(listing.image_id)
    if known is None and mbid is not None:
        caa = caa_cache.stored(mbid)
        if caa is not None and caa.image_url == listing.image_url:
            known = cover_art.Measure(caa.width, caa.height, caa.length, caa.mime)
    return known


def facts(known: cover_art.Measure) -> str:
    """A measurement's facts line, as the picker shows it."""
    size = (
        images.Size(known.width, known.height)
        if known.width is not None and known.height is not None
        else None
    )
    return artwork.describe_parts(size, known.mime, known.length)


def offered(
    mbid: str | None, choices: artwork.Choices | None = None
) -> tuple[tuple[artwork.Candidate, ...], dict[str, formats.EmbeddedArt]]:
    """What the picker offers this album, and the originals already in hand
    (#659): every image the archive lists for its release, its release group,
    and the other releases someone has stepped to, each once.

    From what is stored, never the network — the listings the archive check
    keeps, and the release payload the page already read for the group's id.
    The originals include any image `choices` name that no listing does any
    more, so a choice outlives the listing it was made from.
    """
    candidates: list[artwork.Candidate] = []
    picks: dict[str, formats.EmbeddedArt] = {}
    for listed in caa_listings.listings(mbid):
        listing = listed.listing
        # The group's front is very often one of the release's own images.
        if any(c.image_id == listing.image_id for c in candidates):
            continue
        original = caa_listings.in_hand(mbid, listing)
        image = formats.EmbeddedArt.of(original.data, original.mime) if original else None
        if image is not None:
            picks[listing.image_id] = image
        known = None if image is not None else measurement(mbid, listing)
        candidates.append(
            artwork.Candidate(
                image_id=listing.image_id,
                origin=listed.origin,
                front=listing.front,
                types=listing.types,
                image=image,
                release=listed.release,
                measured=facts(known) if known is not None else None,
                measured_length=known.length if known is not None else None,
                comment=listing.comment,
            )
        )
    for image_id, front in caa_listings.picked_originals(mbid, choices or {}).items():
        picks.setdefault(image_id, formats.EmbeddedArt.of(front.data, front.mime))
    return tuple(candidates), picks


def view(
    album: Album,
    caa: activity_store.CachedCoverArt | None = None,
    archive: formats.EmbeddedArt | None = None,
    *,
    folder_cover: artwork.FolderCoverPolicy,
    chosen: artwork.Source | None = None,
    choices: artwork.Choices | None = None,
    tracks: Sequence[tuple[Path, formats.TrackTags]] | None = None,
) -> artwork.ArtworkView:
    """What the album's artwork is, and what applying it would write (#155).

    One pass over the files, and no MusicBrainz release: artwork facts are disk
    facts, which is why the album page has an endpoint for them rather than
    riding on `/compare`. It is the only part of that page that answers on an
    album Harmonist has not identified yet — the state adopted libraries arrive
    in, and the one where artwork is most often a mess.

    Costs no I/O the tag read wasn't already doing: mutagen parses the whole tag
    block on any open and the image is inside it, so `read_tags` has already
    pulled these bytes off disk and `TrackTags.art` is what it noticed about them.
    The one genuinely new read is the folder cover, once.

    `folder_cover` has NO DEFAULT on purpose (#516). It is the seam between the
    user's setting and a pure function that cannot read it, and the failure it
    guards against is silent: a call site that forgot it would draw a proposal
    to create a `cover.jpg` the user has asked Harmonist not to create, and the
    page would look entirely correct. The album page alone calls this from a
    dozen places; mypy is what makes that a dozen errors rather than a dozen
    chances.
    """
    # Paths as well as tags: the view's plan names its targets by path, exactly
    # as the tagger's does, so the fingerprint a re-tag carries back means the
    # same files on both sides (#469). The same listing the album page's tag
    # read makes.
    #
    # `tracks` is that listing already read, handed over by the album page's
    # comparison (#485). The pass below is what every other caller still makes:
    # the artwork endpoint answers on an album with no MusicBrainz release,
    # where there is no comparison to share one with.
    if tracks is None:
        files = (
            album_files.for_paths(album.folders)
            if album.folders
            else album_files.audio_files(album.path)
        )
        with timing.warn_if_slow(
            "album artwork read", _SLOW_ALBUM_READ, album=album.path, files=len(files)
        ):
            tracks = [(f, formats.read_tags(f)) for f in files]
    sc = album.sidecar
    candidates, picks = offered(sc.mb_release_id if sc else None, choices)
    # The album's stand-in covers, for the release it is matched to now (#663).
    borrowed = {
        b.digest: b.source
        for b in (sc.borrowed_artwork if sc else ())
        if sc is not None and b.release == sc.mb_release_id
    }
    cover = None
    cover_unreadable = False
    if album.cover_path is not None and album.cover_path.exists():
        try:
            data = album.cover_path.read_bytes()
        except OSError:
            log.exception("could not read the folder cover for %s", album.path)
            cover_unreadable = True
        else:
            mime = "image/png" if album.cover_path.suffix.lower() == ".png" else "image/jpeg"
            cover = artwork.FolderCover(
                name=album.cover_path.name,
                image=formats.EmbeddedArt.of(data, mime),
                path=album.cover_path,
                beside=cover_art.covers_beside(album.cover_path),
            )
    # What it carries of a front the archive has since replaced (#886).
    carried = [t.art for _, t in tracks if t.art is not None]
    if cover is not None:
        carried.append(cover.image)
    # A copy squished to fit is the image it was made from (#888).
    originals = derived.originals()
    superseded = artwork.superseded(caa.replaced if caa else None, carried, originals=originals)
    proposed, proposed_images = (
        artwork_watch.proposal(sc, [(p, t.art) for p, t in tracks], cover, folder_cover)
        if not cover_unreadable and not any(t.unreadable for _, t in tracks)
        else ({}, {})
    )
    return artwork.summarise(
        album.path,
        tracks,
        cover,
        caa,
        archive,
        cover_unreadable=cover_unreadable,
        excluded_archive=artwork_watch.excluded(sc, archive),
        chosen=chosen,
        choices={**proposed, **(choices or {})},
        picks={**proposed_images, **picks},
        candidates=candidates,
        borrowed=borrowed,
        superseded=superseded,
        folder_cover=folder_cover,
        # Stat'ed here as the tagger stats them at write time, so the page's
        # plan and the write's are weighed against the same size (#872).
        audio_bytes=album_files.total_bytes(p for p, _ in tracks),
        originals=originals,
    )


def apply(
    album: Album,
    plan: artwork.ArtworkPlan,
    choices: artwork.Choices,
    *,
    scope: artwork.Scope = artwork.Scope.ALL,
    automatic: bool = False,
) -> tagger_mod.ArtworkOutcome:
    """Write the part of `plan`, the one `view` built with `choices`, that
    `scope` permits, and record the outcome.

    Artwork only, and nothing decided here: whether the plan is still the one
    someone looked at, or one nobody need look at, is the caller's to check
    before calling. No MusicBrainz or archive request either: the archive's
    images come from the local cache the plan was read from.
    `tagger.ArtworkChangedError` propagates, before anything is written, when
    the plan's winning image can no longer be found.

    After the write, the album's stand-in record follows what it now carries
    (#663), and a "Better artwork wanted" watch ends if everything planned was
    written. The Activity entry names each file left alone and why, and says
    so when nobody asked for the write (`automatic`, #853). It is recorded in
    whatever action the caller has open.
    """
    mbid = album.sidecar.mb_release_id if album.sidecar else None
    files = (
        album_files.for_paths(album.folders)
        if album.folders
        else album_files.audio_files(album.path)
    )
    outcome = tagger_mod.apply_artwork(
        album.path,
        plan,
        files=files,
        cover_path=album.cover_path,
        archive=cover_art.cached_front(mbid) if mbid else None,
        picks=caa_listings.picked_originals(mbid, choices),
        scope=scope,
    )
    # Which images now stand in for the release's own front (#663). Re-read
    # rather than the scan's copy, and written only if that changed.
    sc = sidecar_mod.read(album.path) if mbid else None
    if mbid and sc is not None and sc.mb_release_id == mbid:
        stand_ins = caa_listings.stand_ins_after(
            album.path, files, mbid, sc.borrowed_artwork, outcome.written
        )
        if stand_ins != sc.borrowed_artwork:
            sidecar_mod.write(album.path, replace(sc, borrowed_artwork=stand_ins))
    if (
        outcome.written
        and not (outcome.stale or outcome.unkept or outcome.failed)
        and artwork_watch.active(album.sidecar)
    ):
        # Writing artwork moves neither the album's release nor its folders,
        # which is all the watch is saved by.
        artwork_watch.save(album, None)
    changed = outcome.changed
    files_ = f"{changed} file{'s' if changed != 1 else ''}"
    if not changed:
        message = "Artwork was already up to date"
    elif automatic:
        message = f"Artwork added automatically to {files_}"
    else:
        message = f"Updated artwork on {files_}"
    # Each file left alone is named, with WHY: the remedy differs — look at a
    # file changed since, or make room in the artwork store (#470) — and a
    # count alone reads as Harmonist having done less than it said.
    notes = [
        f"{why}: {', '.join(names)}"
        for why, names in (
            (
                "changed since it was checked" if automatic else "changed since the page was drawn",
                outcome.stale,
            ),
            (
                "not replaced, because the image there could not be kept for Undo",
                outcome.unkept,
            ),
            ("could not be written", outcome.failed),
        )
        if names
    ]
    # Read after the write, which can move the album's id (event-recording §2).
    album_id = sidecar_mod.album_id_for(album.path) or album.id
    if notes:
        activity.warning(
            f"{message} — {'; '.join(notes)}", album_id=album_id, album_label=album.label
        )
    else:
        activity.info(message, album_id=album_id, album_label=album.label)
    return outcome
