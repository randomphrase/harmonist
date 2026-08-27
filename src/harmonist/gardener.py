"""Whether MusicBrainz has anything for an album that its files don't have yet.

The detector half of #32's metadata gardener, and the whole of what #287's
"Update available" Library filter reads. It answers one question — *would a
re-tag against the release MusicBrainz currently holds change any owned tag?* —
and records the answer on the in-memory `Album`.

## Why this and not the album page's comparison

`compare.album_fields` / `compare.tracklist` are display-shaped: their per-track
vocabulary is Title and Artist, and they deliberately show fields they never
compare (#164). A verdict taken from them would fire on things a re-tag cannot
write and stay silent on most of what one would. `tagger.plan_album` (#266) is
a dry run of the real write, over exactly the fields `owned.Owned` covers, so
the flag, the audit records and Undo all speak the same vocabulary.

## Disk vs. MusicBrainz, never MusicBrainz vs. MusicBrainz

The tempting cheap version compares a fresh payload against the cached one and
calls a difference an update. It is wrong twice over:

* a release that has not moved since we last looked still has an outstanding
  update if the files never took the previous one — so an unchanged payload must
  never clear the flag;
* a release edited and then reverted (A → B → A) has nothing outstanding, and a
  payload comparison would flag it until something cleared it by hand.

Deriving from the plan gets both right for free: the plan is empty exactly when
the files already carry what MusicBrainz says.

That payload comparison still earns its keep as #270's early exit — it decides
whether recomputing is worth the file reads — but it decides *whether to look*,
not what the answer is.

## Nothing here is persisted

No sidecar field, no table, no lifecycle. The flag is a hint that can be rebuilt
from durable state at any time (`warm_from_cache`), so there is nothing to
migrate, nothing to dismiss, and nothing for review-gate item 3 to object to.
The album's state stays derived.

A finding that a human has to *act* on is a different object with a different
lifetime, and it lives in `activity.db` (#271). This is only the flag.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from datetime import timedelta

from . import album_files, formats, mb_cache, tagger
from .models import Album, AlbumState, Release

log = logging.getLogger(__name__)

# How long the warm-up rests between albums. It is bounded by disk rather than
# by MusicBrainz's 1 req/s, so this is not a rate limit — it is politeness to a
# NAS that is also serving Plex, and to the library scan this runs behind.
# Small enough that a 2,000-album library warms in a few minutes.
WARM_PAUSE = timedelta(milliseconds=50)


def would_change(album: Album, release: Release) -> bool:
    """Whether re-tagging `album` against `release` would change an owned tag.

    `TagMismatchError` counts as **True**, not as a failure: the file count no
    longer matching the release's tracklist is a structural change, which is one
    of the loudest things MusicBrainz can do to an album and precisely what the
    user needs to be told about (#266, #267).

    Artwork is out of scope — `cover_path=None`, so the plan leaves each file's
    own art alone and the verdict is about tags only. Asking the Cover Art
    Archive here would spend a separate budget per album per pass, which
    review-gate item 6 forbids and #269 owns instead.

    A superseded tag spelling — MP4's legacy `MUSICBRAINZ_RELEASEID`, which a
    write clears without ever reading back — is deliberately NOT an update, even
    though `formats.has_superseded_tags` exists precisely because `plan_album`
    cannot see it. This filter answers "has MusicBrainz got something the files
    haven't", and a tidy-up Harmonist owes itself is not that. Counting it would
    permanently flag whole swathes of an adopted library for a reason the user
    could do nothing about but re-tag.

    Raises whatever a tag read raises (`formats.READ_ERRORS`) when a file cannot
    be read: that is "I could not tell", which is not the same as "nothing to
    take" and must not be returned as one. `refresh_flag` absorbs it.
    """
    try:
        plan = tagger.plan_album(
            album.path,
            release,
            cover_path=None,
            # Same reading the re-tag button makes (`web/main.py:3829`), so the
            # flag never promises an update that the button would refuse to
            # apply. Without it every INCOMPLETE album raises below and flags
            # permanently — a defect the Incomplete filter already gathers, and
            # not an update MusicBrainz is offering.
            incomplete=album.state == AlbumState.INCOMPLETE,
            # An album can span several directories (#197); planning over the
            # primary one alone would miss whatever the other discs need.
            files=album_files.for_paths(album.folders),
        )
    except tagger.TagMismatchError:
        return True
    return bool(plan.changes)


def refresh_flag(album: Album, release: Release) -> bool:
    """Set `album.update_available` from `release`, and return what it now says.

    The tolerant wrapper every caller uses. A file that cannot be read leaves
    the flag **as it was** rather than clearing it: the album page's comparison
    and the warm-up both run against libraries on network mounts, and a blinking
    mount must not quietly empty the filter. Under-reporting is the direction
    this feature fails in by design (see `Album.update_available`).

    Mutating the Album in place is what makes the flag visible to the Library:
    `ScanRunner.albums()` hands out the live snapshot, so this is the same
    object the grid will render.
    """
    try:
        album.update_available = would_change(album, release)
    except formats.READ_ERRORS:
        # Loud, per the unattended rule: at 3am the log is the only channel.
        # ERROR rather than WARNING — nothing recovered, we simply cannot say
        # whether this album has an update, and it will keep reporting whatever
        # it last said until something can read it.
        log.exception("could not tell whether %s has a tag update available", album.path)
    return album.update_available


def warm_from_cache(albums: Iterable[Album], *, pause: timedelta = WARM_PAUSE) -> int:
    """Rebuild the update-available flags from stored releases. Returns how many
    albums were flagged.

    The flag lives in process memory, so a restart blanks it and the filter
    would read zero until the background pass next came round — potentially a
    whole sweep interval on a NAS, which reads as a broken control rather than
    an empty one.

    It does not have to. Both inputs to the verdict survive a restart: the
    release payload is in `mb_release_cache` and the tags are on disk. So this
    reaches the *same* answer the pass would, with **zero MusicBrainz
    requests** — the pass's fetch exists only to refresh the payload this is
    already reading. The half being skipped is the rate-limited one: a full pass
    is bounded by 1 req/s (~35 minutes for 2,000 albums), this is bounded by
    disk.

    An album MusicBrainz was never asked about has no row and simply keeps its
    False — see the under-reporting note on `Album.update_available`.

    Blocking I/O (one read per file, per `plan_album`), so call it from a worker
    thread, never the event loop.
    """
    flagged = 0
    looked = 0
    for album in albums:
        sc = album.sidecar
        if sc is None or not sc.mb_release_id:
            continue  # nothing to compare against; not an error
        release = mb_cache.stored_release(sc.mb_release_id)
        if release is None:
            continue  # never fetched, or fetched under a different `inc`
        looked += 1
        if refresh_flag(album, release):
            flagged += 1
        if pause > timedelta(0):
            time.sleep(pause.total_seconds())
    log.info(
        "update-available warm-up: %d of %d albums with a stored release have an update",
        flagged,
        looked,
    )
    return flagged
