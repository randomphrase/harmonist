"""Whether MusicBrainz has anything for an album that its files don't have yet.

The detector half of #32's metadata gardener, and the whole of what #287's
"Update available" Library filter reads. It answers one question — *would a
re-tag against the release MusicBrainz currently holds change any owned tag?* —
and records the answer on the in-memory `Album`.

Three callers ask it, and they differ only in where the release comes from: the
album page, from the cache as it renders; `warm_from_cache`, from every stored
payload after a restart, spending no requests; and `sweep` (#270), from a fresh
fetch on a timer, which is the only one that can find an update nobody has gone
looking for. `sweep` writes nothing to anybody's files — see its docstring.

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

## Nothing computed here is persisted

No sidecar field, no table. The flag, its significance and the version they were
reached against are all hints that can be rebuilt from durable state at any time
(`warm_from_cache`), so there is nothing to migrate and nothing for review-gate
item 3 to object to. The album's state stays derived.

The one thing that IS stored is the one thing nothing can recompute: that the
user pressed **Ignore**, and which version of the release they pressed it
against (#271). That is a decision, not a derivation — and deliberately a
*bookmark* rather than a rejection, because MusicBrainz is canonical
(`docs/design/principles.md`): the answer to disliking what it says is to go and edit it,
and an ignore exists to keep quiet until that lands. `is_ignored` is the whole
of the reading side; the flag itself never consults it.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import (
    activity,
    activity_store,
    album_files,
    contributions,
    formats,
    mb_cache,
    mb_lookup,
    progress,
    request_load,
    tag_history,
    tagger,
)
from .config import GardenerConfig, ReviewFrom
from .formats import owned
from .models import Album, AlbumState, Release
from .transforms import TaggingChoices

log = logging.getLogger(__name__)

_LIBRARY_STATES = frozenset({AlbumState.COMPLETE, AlbumState.INCOMPLETE})

# How often a running pass says where it has got to. Long enough not to fill the
# log on a healthy install, short enough that a stalled pass is obvious.
_PROGRESS_EVERY = timedelta(seconds=30)

# The longest a pass waits for the web server to go idle before it looks at one
# more album anyway (#793). A request that takes longer than this — a re-tag on
# a slow disk — then shares the machine with one album in every ten seconds,
# and one that never ends cannot park the pass.
_LONGEST_WAIT = timedelta(seconds=10)


SettingsReader = Callable[[], TaggingChoices]
RoutingReader = Callable[[], GardenerConfig]

_settings: SettingsReader | None = None
_routing: RoutingReader | None = None


def configure(settings: SettingsReader | None) -> None:
    """Hand the check a reader of the user's spelling settings (#685).

    A reader rather than a value, so the check judges each album under the
    settings as they stand at that moment: Settings applies without a restart,
    and a copy taken at startup would have the Library flagging albums against
    a choice the user has since changed. `create_app` installs one over its
    live config; `None` means `TaggingChoices()`, MusicBrainz's own spelling.
    """
    global _settings
    _settings = settings


def configure_routing(routing: RoutingReader | None) -> None:
    """Hand the check a reader of where updates go (#273): which are applied
    and which wait in the Inbox. Live for the reason `configure`'s reader is.
    `None` means the shipped levels."""
    global _routing
    _routing = routing


def _choices() -> TaggingChoices:
    return _settings() if _settings is not None else TaggingChoices()


def _routing_now() -> GardenerConfig:
    return _routing() if _routing is not None else GardenerConfig()


# Held from reading the setting to recording the verdict, so a look that began
# under the old setting cannot land AFTER the re-check a settings change starts
# (#685) and leave its stale verdict standing. One album's plan at a time, which
# the threads that share it — the warm-up, the re-check, the pass, a page view —
# barely notice.
_flag_lock = threading.Lock()


def plan_for(album: Album, release: Release) -> tagger.AlbumPlan:
    """What re-tagging `album` against `release` would change, without writing.

    The plan is both the verdict and the explanation: a non-empty `changes` is
    the update-available flag, and the very same object renders the field-by-
    field account the album page shows (#291). One computation, so the Library
    and the album page can never disagree about whether there is anything to
    take — the confusion #291 was filed for.

    `TagMismatchError` propagates rather than being folded into an empty plan:
    the file count no longer matching the release's tracklist is a structural
    change, one of the loudest things MusicBrainz can do to an album, and it
    counts as an update (#266, #267). `update_flag` is what turns it into one.

    Artwork is out of scope, and `artwork=False` is how that gets SAID. It used
    to be implied by `cover_path=None`, on the reasoning that no cover meant
    nothing to write — which was true until the Cover Art Archive became a
    second source needing no folder cover (#442). The plan then started carrying
    an ARTWORK change that `owned.ranked` raises on by design, and this call
    became a 500 for any album whose archive cover happened to be cached. A
    proxy that holds is still a proxy (#448).

    Asking the archive itself here would spend a separate budget per album per
    pass, which review-gate item 6 forbids and #269 owns instead; the flag also
    saves the read of every file's embedded art, which is not free on a pass
    that touches the whole library.

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

    Under the user's transforms as they stand now (#685), because the plan is a
    dry run of the write and the write applies them: an album already carrying
    the spelling they chose has nothing to take, and one carrying the other has
    a `SETTINGS` change.
    """
    return tagger.plan_album(
        album.path,
        release,
        cover_path=None,
        artwork=False,
        # Same reading the re-tag button makes (`web/main.py:3829`), so the
        # flag never promises an update that the button would refuse to apply.
        # Without it every INCOMPLETE album raises `TagMismatchError` and flags
        # permanently — a defect the Incomplete filter already gathers, and not
        # an update MusicBrainz is offering.
        incomplete=album.state == AlbumState.INCOMPLETE or bool(album.included_pregap_count),
        # An album can span several directories (#197); planning over the
        # primary one alone would miss whatever the other discs need.
        files=album_files.for_paths(album.folders),
        tagging=_choices(),
    )


@dataclass(frozen=True)
class Assessment:
    """What one look at an album against a release found.

    Two fields, because a caller acting on the answer has to tell three
    situations apart and neither field can do it alone:

    * `plan` set, `verdict` None — the files already carry what MusicBrainz
      says. Positively nothing outstanding, which is the only answer that may
      be used to retire a finding (#271).
    * `plan` set, `verdict` a level — an update is outstanding, and the level
      says how far it reaches.
    * `plan` None — no plan could be built. `verdict` then separates the two
      ways that happens: STRUCTURE when the release no longer fits the files (a
      real finding, and one of the loudest things MusicBrainz can do), None when
      the files could not be READ, which is not an answer at all and must not be
      mistaken for either of the others.
    """

    plan: tagger.AlbumPlan | None
    verdict: owned.Significance | None


def _countable(plan: tagger.AlbumPlan) -> Iterator[tuple[Path, str, Any, Any]]:
    """The entries of `plan` that count as an update, as `(path, field, before,
    after)` — the path because what a file's artist may be spelled as depends on
    its own track's credit (#678).

    Not every entry does, and there are two ways an entry can fail to.

    **A credit list arriving with one name in it** (#337). That is a change to
    make while tagging anyway and not a reason to tag: `album_artist` beside it
    already says the same thing, and a player without the list falls back to the
    phrase. `albumartists` is new in Picard too, so no existing library carries
    it — left counted, one field put every album in a real library into the
    Inbox.

    **A second correct country** (#346). The file carries `DE` where the
    release's scalar `country` is `GB` — both true of this release, and what
    Picard writes when told to. Left counted, EVERY album in such a library is
    an update, permanently, and the first night of #32 empties the whole
    library into the Inbox.

    The album title's second spelling was filtered here too (#283), and is
    counted now (#685), as are the artist names' (#678): a setting chooses
    between those spellings, so the one on disk either follows the user's
    setting or it is an update of the `SETTINGS` level. Nothing chooses a
    country, so there is nothing for that one to converge on.

    Filtered HERE rather than in `owned.diff` or `tagger._changes_for`, and that
    is the whole of the design: `plan.changes` is also what the activity record
    and the undo are built from, so a re-tag that rewrites the field must still
    say that it did (#545 — it did not, and Undo could not put the title back).
    The album page keeps showing it as a pending change for the same reason —
    the page says what a re-tag would do, this says whether you need one.

    One definition for the flag and the verdict both, so an album cannot be
    flagged for a change no finding can be raised about, or raised about a
    change the Library never showed.
    """
    for path, changes in plan.changes.items():
        for field, (before, after) in changes.items():
            if plan.is_second_spelling(field, before):
                continue
            if not owned.is_opportunistic(field, before, after):
                yield path, field, before, after


def verdict_for(plan: tagger.AlbumPlan) -> owned.Significance | None:
    """How far the outstanding change in `plan` reaches, or None if none does.

    The **furthest-reaching** entry wins (`owned.ORDER`), because that is what
    decides how much of the album is in question: an ISRC arriving beside a
    retitle does not make the retitle an enrichment.

    A `KeyError` from an unclassified field and a `ValueError` from artwork both
    propagate, and neither is defended against here. Artwork cannot reach this:
    `plan_for` passes `cover_path=None`, so `_changes_for` records no `ARTWORK`
    entry (#269 owns cover art, on its own cadence and its own budget). If one
    ever did arrive it would need a verdict of its own rather than a rank
    pretending to compare with a retitle — so the loud failure is the correct
    outcome, and the pass's boundary catch turns it into one.
    """
    levels = [plan.significance_of(*entry) for entry in _countable(plan)]
    return max(levels, key=owned.ranked) if levels else None


def follows_settings(plan: tagger.AlbumPlan) -> bool:
    """Whether any change in `plan` is the user's own setting arriving (#685).

    What the album page says out loud beside the update — the change did not
    come from MusicBrainz, so its remedy is the setting rather than an edit
    upstream or an Ignore.
    """
    return any(
        plan.significance_of(*entry) is owned.Significance.SETTINGS for entry in _countable(plan)
    )


def refresh_flag(album: Album, release: Release) -> Assessment:
    """Set `album`'s update fields from `release`; return what was found.

    Three fields move together and are meant to be read together:
    `update_available` (is there anything to take), `update_significance` (how
    far it reaches), and `mb_version` (which payload both of those are true of).
    Setting them apart is how the Library would come to show a verdict from one
    release beside a version from another.

    The tolerant wrapper every caller uses. A file that cannot be read leaves
    them **as they were** rather than clearing them: the album page's comparison
    and the warm-up both run against libraries on network mounts, and a blinking
    mount must not quietly empty the filter. Under-reporting is the direction
    this feature fails in by design (see `Album.update_available`).

    **A missing plan is not "nothing to take"**, and the two ways of missing one
    are not the same either — see `Assessment`, which is the whole reason this
    returns one rather than the plan alone. (The structural case is not left
    unexplained on screen: the album page reports it through `_shape_mismatch`
    and `absent_media`, which say far more about it than a field diff could.)

    Mutating the Album in place is what makes this visible to the Library:
    `ScanRunner.albums()` hands out the live snapshot, so this is the same
    object the grid will render.

    Under `_flag_lock`, from reading the setting to recording the verdict.
    """
    contributions.observe(album, release, mb_cache.fetched_at(str(release["id"])))
    with _flag_lock:
        return _assess(album, release)


def _assess(album: Album, release: Release) -> Assessment:
    """`refresh_flag`'s body, which the caller holds `_flag_lock` around."""
    try:
        plan = plan_for(album, release)
    except tagger.TagMismatchError as e:
        if isinstance(e, tagger.TrackAssignmentRequired):
            album.unassigned_track_count = e.unassigned
        album.update_available = True
        album.update_significance = owned.Significance.STRUCTURE
        # No field diff exists to describe it, so the reason stands in for one.
        album.update_changes = album.update_lead = ()
        album.update_note = _mismatch_note(e)
        album.mb_version = release_version(release)
        return Assessment(plan=None, verdict=owned.Significance.STRUCTURE)
    except formats.READ_ERRORS:
        # Loud, per the unattended rule: at 3am the log is the only channel.
        # ERROR rather than WARNING — nothing recovered, we simply cannot say
        # whether this album has an update, and it will keep reporting whatever
        # it last said until something can read it.
        log.exception("could not tell whether %s has a tag update available", album.path)
        return Assessment(plan=None, verdict=None)
    verdict = verdict_for(plan)
    album.unassigned_track_count = plan.unassigned
    album.update_available = verdict is not None
    album.update_significance = verdict
    album.update_changes, album.update_lead = (
        _change_rows(album, plan, release) if verdict is not None else ((), ())
    )
    album.update_note = None
    album.mb_version = release_version(release)
    return Assessment(plan=plan, verdict=verdict)


#: How many rows the Inbox leads with before its full list.
LEAD_ROWS = 2


def _mismatch_note(e: tagger.TagMismatchError) -> str:
    """What the Inbox says for an update no field diff describes: what the
    album needs, and the counts that disagree when they do."""
    tracks = "track" if e.tracks == 1 else "tracks"
    counts = f"MusicBrainz lists {e.tracks} {tracks}, {e.files} on disk"
    if isinstance(e, tagger.TrackAssignmentRequired):
        review = "Track assignments need review"
        return f"{review} · {counts}" if e.files != e.tracks else review
    return counts


def _change_rows(
    album: Album, plan: tagger.AlbumPlan, release: Release
) -> tuple[tuple[tag_history.FieldChange, ...], tuple[tag_history.FieldChange, ...]]:
    """History's rows for the changes in `plan` that count as an update, in
    History's order, and the `LEAD_ROWS` of them to lead with: the
    furthest-reaching first and, within a level, in the order every change list
    uses (`tag_history.field_rank`). One row more than that leads with all of
    them, as they are: the row costs the line a Show all button would take.

    The same rows History will show once the update is taken (`from_plan`), so
    the Inbox describes a waiting update in the words and order its History
    entry will use. Only the counted fields: an entry `_countable` sets aside is
    written by a re-tag but is not why the album is waiting, and leading with it
    would explain the wrong thing. Id rows carry the names `release` has for
    their ids (#801), from the payload the check already holds.
    """
    levels: dict[str, owned.Significance] = {}
    for entry in _countable(plan):
        level = plan.significance_of(*entry)
        field = entry[1]
        if field not in levels or owned.ranked(level) > owned.ranked(levels[field]):
            levels[field] = level
    rows = tag_history.named(
        [
            row
            for row in tag_history.from_plan(plan, album.path, album_files.for_paths(album.folders))
            if row.field in levels
        ],
        tagger.change_names(release),
    )
    if len(rows) <= LEAD_ROWS + 1:
        return rows, rows
    lead = sorted(
        rows, key=lambda row: (-owned.ranked(levels[row.field]), tag_history.field_rank(row.field))
    )
    return rows, tuple(lead[:LEAD_ROWS])


def warm_from_cache(
    albums: Sequence[Album],
    *,
    ignored: Mapping[str, activity_store.IgnoredUpdate],
    held: Callable[[Album], bool] = lambda _: False,
) -> int:
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

    **Out of the way of requests, and audible while it runs** (#299). It shipped
    doing neither, and the two failures compounded: on a NAS it took the CPU for
    however long a whole library takes to parse, while saying nothing at all
    until it finished — so an album page that crawled during that window looked
    identical to one that had hung, with no way to tell which from the log.

    **It closes with the counts the page shows** (#818): the albums the
    Library's Tag updates filter lists and how many of them the Inbox holds,
    which is what `ignored` and `held` are for. It counted every flag it set,
    ignored updates included, and so named a number nothing on screen did.
    """

    def closing(looked: int, flagged: int) -> str:
        waiting = sum(waiting_by_level(albums, ignored).values())
        review = len(for_review(albums, ignored, _routing_now(), held))
        found = _albums(waiting, "has a tag update", "have a tag update")
        return f"{found}, {review} for review" if review else found

    return _refresh_from_cache(
        albums,
        wanted=lambda _: True,
        what="warm-up",
        label="Checking for tag updates",
        closing=closing,
    )


def _albums(n: int, one: str, many: str) -> str:
    return f"{n} album {one}" if n == 1 else f"{n} albums {many}"


def recheck_rebuilt(albums: Sequence[Album]) -> int:
    """Re-judge albums a rescan rebuilt (#758). Returns how many were flagged.

    A rebuilt album starts with no flag. That is right when the edit that
    rebuilt it took the update, and wrong in every other case — a genre set in
    another tool, a Picard re-tag that kept a title of its own — where the
    album would leave the filter with the update still outstanding, and stay
    out until it was opened or Harmonist restarted. `sweep` would not put it
    back either: it re-judges only when MusicBrainz's payload has moved.

    The warm-up's loop over just those albums, so it reads only their files
    and, like the warm-up, spends no MusicBrainz requests.
    """
    # Out of the status bar: it follows every rescan, usually over a handful of
    # albums, and the bar has just said "Scanning" (#757).
    return _refresh_from_cache(albums, wanted=lambda _: True, what="rescan re-check", label=None)


def recheck_for_settings(
    albums: Sequence[Album], before: TaggingChoices, after: TaggingChoices
) -> int:
    """Re-judge the albums a change to the user's spelling settings can move
    (#685, #678). Returns how many albums were flagged.

    The warm-up's loop, from the same stored releases and so for the same zero
    MusicBrainz requests, narrowed to albums whose release a tagging would write
    differently under `after` than under `before`. That is decided from the
    release alone — no file is read to decide it — and exactly: whatever a new
    setting touches, an album it cannot move is one whose tags come out the
    same. The rest cannot have moved, and re-reading them would be a
    whole-library pass over a network mount for a click in Settings.

    Records nothing in the Activity feed: the user caused these updates a moment
    ago, and an album with nothing but a Settings change is never announced.
    """
    changed_groups = {group for _, group in before.library_releases ^ after.library_releases}
    return _refresh_from_cache(
        albums,
        wanted=lambda release: (
            (release.get("release-group") or {}).get("id") in changed_groups
            or tagger.tagsets_for(release, before) != tagger.tagsets_for(release, after)
        ),
        what="settings re-check",
        label="Re-checking tags",
        # Only the albums the setting can move are looked at, so the count is of
        # those, not of the library: a library-wide "0 albums have a tag update"
        # would be false of every album with an update from MusicBrainz.
        closing=lambda looked, flagged: (
            f"{_albums(looked, 'affected', 'affected')}, {flagged} with a tag update"
            if looked
            else "no albums affected"
        ),
    )


def _stored_release_of(album: Album) -> Release | None:
    """The stored payload of the album's release, or None when there is nothing
    to compare against: no release yet, or one never fetched (or fetched under a
    different `inc`). Neither is an error."""
    sc = album.sidecar
    if sc is None or not sc.mb_release_id:
        return None
    return mb_cache.stored_release(sc.mb_release_id)


def _refresh_from_cache(
    albums: Sequence[Album],
    *,
    wanted: Callable[[Release], bool],
    what: str,
    label: str | None,
    closing: Callable[[int, int], str] | None = None,
) -> int:
    """The paced loop `warm_from_cache` and the re-checks share.

    Shown in the status bar as `label` while it runs (#757): the warm-up and the
    Settings re-check are minutes of file reads on a large library, and a person
    may be waiting on either. None keeps it out of the bar. A shown job ends with
    `closing(looked, flagged)`.
    """
    started = time.monotonic()
    log.info("update-available %s: checking %d albums", what, len(albums))
    flagged = 0
    looked = 0
    reported = started
    shown = (
        progress.job(label, total=len(albums))
        if label is not None
        else contextlib.nullcontext(progress.Job("", len(albums)))
    )
    with shown as job:
        for album in albums:
            release = _stored_release_of(album)
            if release is not None and wanted(release):
                looked += 1
                _yield_to_requests()
                refresh_flag(album, release)
                # The verdict is the flag, not the return value — a None comes
                # back both for an unreadable album (not flagged) and for one
                # whose tracklist no longer fits (flagged), so counting the
                # return would undercount.
                if album.update_available:
                    flagged += 1
            job.advance()
            if time.monotonic() - reported >= _PROGRESS_EVERY.total_seconds():
                reported = time.monotonic()
                log.info(
                    "update-available %s: %d albums looked at, %d with an update",
                    what,
                    looked,
                    flagged,
                )
        job.closing = closing(looked, flagged) if closing is not None else None
    log.info(
        "update-available %s: done in %.0fs — %d of %d albums with a stored release have an update",
        what,
        time.monotonic() - started,
        flagged,
        looked,
    )
    return flagged


def _yield_to_requests() -> None:
    """Before each album, wait until no web request is being served (#793).

    The work is pure Python — mutagen parsing, the plan's diff — so it holds the
    GIL, and a request served alongside it gets a fraction of the machine. That
    was #299, an album page that crawled after every restart. The answer then
    was to rest three times as long as each album took, always: on a NAS, five
    minutes of work became twenty, with or without anyone to be in the way of.

    Only a request in flight needs the machine, so that is what this waits on.
    A request arriving mid-album shares it for the rest of that one album and
    then has it to itself, which beats the duty cycle: under that, a slow album
    page competed with the pass for its whole length.
    """
    request_load.wait_for_idle(_LONGEST_WAIT)


# --- The background pass (#270) ---------------------------------------------

#: How long an album's release payload may go unasked-about before the pass asks
#: MusicBrainz again. A week rather than a night: edits arrive on the scale of
#: months for most releases, so noticing one six days late costs nothing, while
#: asking six times too often costs six times the budget for the same answer.
RECHECK_AFTER = timedelta(days=7)

#: How long a full sweep of everything currently due should take (#349). The
#: *goal*, from which the rate is derived — not a rate that happens to reach the
#: goal, which is what the hand-set hundred-an-hour cap this replaces was.
#:
#: Deriving is what makes it self-stabilising. A large backlog — a first run,
#: where the whole library is due at once — yields a proportionally larger slice
#: and still clears inside a day; steady state settles at exactly
#: `len(library) / RECHECK_AFTER` a day, which is the demand and not a multiple
#: of it. The old pair provisioned 2,400 fetches a day against a demand of ~286
#: for a 2,000-album library, and spent them in hundred-request bursts.
#:
#: **Meaningfully shorter than `RECHECK_AFTER`, and that is not a taste.** The
#: backlog settles where inflow meets outflow: albums come due at
#: `len(library) / RECHECK_AFTER` a day and are cleared at `due / SWEEP_WINDOW`,
#: so a day-long window holds the queue at a seventh of the library and the lag
#: at about a day. Set it to a week and the equilibrium is the whole library
#: permanently overdue. #273 exposes it; the arithmetic is why it should not be
#: set much higher.
SWEEP_WINDOW = timedelta(hours=24)

#: How often a tick fires, and so how big a slice one gets: a tick is
#: `SWEEP_TICK / SWEEP_WINDOW` of what is due, which at these values is 1/144 —
#: two or three albums for a library of a couple of thousand.
#:
#: Ten minutes rather than the hour it replaces, because the tick length IS the
#: burst length. An hourly tick doing the same daily total still delivers it as
#: twelve back-to-back requests and then nothing; at ten minutes there is no
#: burst left to speak of. Short enough, too, that the pass stands aside for a
#: sync within ten minutes rather than an hour — the guards are re-read per
#: tick.
#:
#: **Not jittered, deliberately.** A slice this small has no burst to spread and
#: nothing to fall into lockstep with: what lands together is two or three
#: albums, which is what a tick drains anyway. The pattern worth breaking up was
#: the hundred-album one — `_due` orders off the fetch times an earlier pass
#: wrote, so a burst comes due again as a burst one `RECHECK_AFTER` later, and
#: the shape repeats forever — and sizing the slice is what breaks it, not
#: randomising the clock. Between a start time nobody coordinates and a rate
#: that already varies with the library, there is enough spread here without a
#: knob to explain.
#:
#: Lives here beside the window rather than in `web/main.py` (where it was, as
#: `_UPDATE_CHECK_INTERVAL`) precisely because `sweep` now has to know it: the
#: slice is a fraction of the window, so the constant that *schedules* the pass
#: and the constant the pass *divides by* must be the same one. Two constants in
#: two modules that had to be kept in step is how the rate drifted to 8x the
#: goal in the first place.
SWEEP_TICK = timedelta(minutes=10)

#: The most of a tick that may be spent fetching, so a slice always fits inside
#: the tick that scheduled it. At the 1 req/s MusicBrainz floor a slice is
#: roughly one second an album, and a library big enough for `SWEEP_WINDOW` to
#: hand out more albums than that would run each pass into the next — which the
#: lock refuses, so the excess is silently dropped anyway. Better to bound it
#: here, where the reason is written down.
_TICK_FETCH_BUDGET = 0.5

#: Seconds one MusicBrainz request occupies. Not a limiter — `musicbrainzngs`
#: owns the actual 1 req/s floor — just the figure `_slice_size` costs a fetch
#: at when deciding how many fit in a tick.
_SECONDS_PER_FETCH = 1.0

#: The most new updates one pass names in the Activity feed, one linked entry
#: each (#600). Beyond this it posts a single counted line instead. A pass
#: normally finds one or two, so the cap is for bursts: a wave of MusicBrainz
#: edits, or the first pass on a new install.
NAMED_UPDATES_MAX = 5

#: How many MusicBrainz failures in a row end a pass early. MusicBrainz being
#: down looks exactly like this, and grinding through the rest of the queue to
#: fail at each one spends a request apiece and buries the first failure — the
#: only one that says anything — under ninety-nine identical ones.
_GIVE_UP_AFTER = 5

#: The failure run, ACROSS ticks (#349). It was a local of `sweep`, which was
#: sound while a pass was a hundred albums and is dead code now that one is
#: two or three: a counter that resets every ten minutes can never reach five,
#: so an outage would be met with a fresh slice of requests every tick, forever,
#: which is the opposite of what #349 is for. Held here, five failures end the
#: tick they land in and every tick after it ends on its first failure — one
#: request and one line per ten minutes — until something succeeds.
#:
#: In memory, like `_asked`: a restart re-learns it at the cost of one slice.
_consecutive_failures = 0

#: Sorts before every real timestamp, so "never asked" is the stalest thing
#: there is and an album nobody has ever looked at goes to the front of the
#: queue. Those are the albums the flag is silent about (see
#: `Album.update_available`), which makes them the ones a pass is worth most on.
_NEVER = datetime.min.replace(tzinfo=UTC)

#: When this process last ASKED about a release id, as distinct from when a
#: payload was last stored under one (`mb_cache.fetch_times`). Normally the same
#: fact; two cases separate them, and in both the album is asked about on every
#: pass forever without this:
#:
#: * a **merged** release — the fetch follows MusicBrainz's redirect and the row
#:   lands under the id it gave back (#268), so the id the sidecar names never
#:   gets a fresher row;
#: * a **deleted** release — the fetch raises and nothing is stored at all,
#:   which is right (a negative is never cached), but leaves the same gap on an
#:   album whose remedy is a human decision that re-asking brings no closer.
#:
#: In memory, because it paces a pass rather than describing the library:
#: losing it on a restart costs one extra request for each affected album and
#: nothing else. One entry per distinct release id asked about, so it is bounded
#: by the size of the library — the same order as the album list itself.
_asked: dict[str, datetime] = {}

#: When the scheduler will next start a pass, as it last said (#623). Set from
#: `periodic.run_periodically`'s `on_schedule`; None until the timer is running,
#: which it never is under a bare TestClient.
_next_pass: datetime | None = None


def note_next_pass(at: datetime) -> None:
    """The scheduler's hook: the next pass is due at `at`."""
    global _next_pass
    _next_pass = at


@dataclass(frozen=True)
class SweepStatus:
    """Where the background pass is, for the Settings page (#623)."""

    matched: int  # albums with a release to ask about
    due: int  # of those, albums whose release has gone unasked-about too long
    next_pass: datetime | None
    backing_off: bool  # MusicBrainz failing: each pass stops at its first failure

    @property
    def checked(self) -> int:
        return self.matched - self.due


def status(albums: Sequence[Album], *, now: datetime | None = None) -> SweepStatus:
    """Counts from the same stored fetch times the pass schedules off: one
    database read, no MusicBrainz request."""
    now = now or datetime.now(UTC)
    due = sum(len(group) for _, group in _due(albums, recheck_after=RECHECK_AFTER, now=now))
    matched = sum(1 for a in albums if a.sidecar is not None and a.sidecar.mb_release_id)
    return SweepStatus(
        matched=matched,
        due=due,
        next_pass=_next_pass,
        backing_off=_consecutive_failures >= _GIVE_UP_AFTER,
    )


@dataclass(frozen=True)
class PassResult:
    """What one pass did — the log line reads off this, and so will #274's
    digest."""

    #: Releases fetched from MusicBrainz. The pass's whole budget cost.
    asked: int
    #: Of those, the ones whose files were then read — because the payload had
    #: moved, or because there was nothing stored to compare it against. The gap
    #: between this and `asked` is the early exit doing its job.
    examined: int
    #: Of those, the ones that turned out to have an update outstanding.
    flagged: int
    #: Of those, the ones that did NOT have one before this pass looked. The
    #: digest counts these and not `flagged`, and the difference is the whole of
    #: #274's discipline: an album the user has been sitting on for a fortnight
    #: is still outstanding every time MusicBrainz touches the release, so
    #: `flagged` would announce it again on an edit that changed nothing for
    #: them. A transition is news; a standing state is not (#272). The albums
    #: themselves rather than a count, so the digest can name them (#600).
    newly_flagged: tuple[Album, ...]
    #: Releases MusicBrainz no longer has (#194/#210).
    gone: int
    #: Fetches that failed — a network error, a 503. Not an answer either way.
    failed: int
    #: Whether the pass stopped early because MusicBrainz kept failing. Read by
    #: the closing log line, so a short pass says why it was short rather than
    #: leaving a small number of albums looking like a quiet night.
    gave_up: bool = False


def _slice_size(due_count: int, *, tick: timedelta, window: timedelta) -> int:
    """How many of `due_count` releases this tick's share of `window` is worth.

    The whole of #349's rate change, and deliberately one line of arithmetic
    with its reasoning around it rather than a constant somebody chose. A tick
    is `tick / window` of the goal, so the pass asks about that fraction of
    whatever is outstanding — which means the rate answers to the size of the
    backlog instead of to a number picked when the library was a different size.

    Rounded **up**, so a due list smaller than the ratio still moves. Left at
    exactly zero it would stall: a one-album backlog is 1/144th of a tick's
    worth, and a floor would never let that album be asked about at all.

    Capped so the slice fits inside the tick that scheduled it (see
    `_TICK_FETCH_BUDGET`). A pass that outran its tick would be refused by the
    lock rather than queued, so the excess is dropped either way; bounding it
    here is the version that has a reason attached.
    """
    share = math.ceil(due_count * (tick / window))
    fits = int(tick.total_seconds() * _TICK_FETCH_BUDGET / _SECONDS_PER_FETCH)
    return max(0, min(share, fits))


def sweep(
    albums: Sequence[Album],
    *,
    tick: timedelta = SWEEP_TICK,
    window: timedelta = SWEEP_WINDOW,
    limit: int | None = None,
    recheck_after: timedelta = RECHECK_AFTER,
) -> PassResult:
    """Ask MusicBrainz about the albums whose release we have looked at least
    recently, and update their flags from what comes back.

    The scheduled half of #32, and **detect-only**: it fetches, compares and
    sets `album.update_available` and `album.update_significance`, and writes
    nothing to anybody's files. That is not a stage of a plan, it is what the
    classifier currently permits — `owned.AUTO_APPLY` is empty, so every change
    needs a person. Two things fall out of that: the pass cannot damage a
    library, and its whole value is that the Library's Update available filter
    stops depending on somebody having happened to open the album (#287, #293).

    **It keeps asking about an album the user has ignored** (#271), which is not
    an oversight. Ignoring says *mute this until MusicBrainz moves*, so asking
    is the only thing that can ever un-mute it. Nothing about an ignore is read
    here: the flag stays a plain fact about the files against MusicBrainz, and
    the surfaces subtract the ignored albums when they draw themselves.

    **The early exit is the design.** The stored payload is read BEFORE the
    fetch, and an unchanged one ends the album there — no file reads, no plan,
    nothing recorded. So a second pass over a library MusicBrainz has not
    touched costs its requests and nothing else, which is #32's idempotency
    invariant enforced by construction rather than by care.

    An unchanged payload never *clears* a flag, only skips it. The two are easy
    to conflate and the difference is a bug: an album whose files never took the
    previous update still has one outstanding, however long MusicBrainz has sat
    still (see this module's header).

    `max_age=FRESH`, which no other read-only caller passes. Serving this the
    cached row would make the pass a no-op that costs nothing and reports
    nothing — going and looking IS the operation, and the refreshed row it
    leaves behind is what dates the next pass's queue.

    **How many it asks about is derived, not chosen** (#349). One tick gets
    `tick / window` of whatever is currently due, so the pass sweeps everything
    outstanding across `window` and no faster — see `_slice_size`. `limit`
    overrides that outright, for a caller (and for tests) that means a specific
    number rather than a rate; nothing in the app passes it.

    Blocking on both the network and the disk, so call it from a worker thread,
    never the event loop.
    """
    global _consecutive_failures
    now = datetime.now(UTC)
    due = _due(albums, recheck_after=recheck_after, now=now)
    slice_size = limit if limit is not None else _slice_size(len(due), tick=tick, window=window)
    # DEBUG, not INFO: at one tick every ten minutes this line is 144 a day on a
    # library with nothing to say. What a healthy pass has to report is the
    # closing summary, and that only speaks up when there is something in it.
    log.debug(
        "update check: %d album(s) due, asking MusicBrainz about up to %d", len(due), slice_size
    )
    asked = examined = flagged = gone = failed = 0
    newly_flagged: list[Album] = []
    gave_up = False
    reported = time.monotonic()
    for mbid, group in due[:slice_size]:
        # Read the baseline BEFORE the fetch: `fetch_release` replaces the row
        # on its way back, so afterwards there is nothing left to compare with.
        before = mb_cache.stored_release(mbid)
        # Recorded whatever happens next, including a failure. This is "we spent
        # a request on this id", which is the thing that must not repeat every
        # pass — and the ids it protects are exactly the ones a fetch does not
        # leave a row for.
        _asked[mbid] = datetime.now(UTC)
        asked += 1
        try:
            release = mb_cache.fetch_release(mbid, max_age=mb_cache.FRESH)
        except mb_lookup.ReleaseGoneError:
            # An answer, not a failure — so it does not count towards giving up.
            # INFO rather than WARNING on purpose: a release deleted last year is
            # still deleted tonight, and a mirrored WARNING would post that
            # non-news to the Activity feed every pass forever. The album page
            # already says so whenever the user looks (#194/#210), and #271 is
            # what turns it into something waiting for them.
            log.info("update check: MusicBrainz no longer has release %s", mbid)
            gone += 1
            _consecutive_failures = 0
            continue
        except mb_lookup.MBError:
            failed += 1
            _consecutive_failures += 1
            # Kept out of the Activity feed (`_diagnostic`): nothing was lost,
            # the next pass retries, and a MusicBrainz wobble is not the user's
            # to act on. The log still has it, with the traceback.
            log.warning(
                "update check: could not fetch release %s",
                mbid,
                exc_info=True,
                extra={"_diagnostic": True},
            )
            if _consecutive_failures >= _GIVE_UP_AFTER:
                # Only the FIRST tick to give up says so out loud. This warning
                # IS mirrored into the Activity feed — a check that stopped
                # looking is a thing the user can only find out from us — and
                # the counter now spans ticks (#349), so without the transition
                # test a sustained outage would post the same non-news every ten
                # minutes for as long as it lasted. The same "one line per
                # episode" rule `run_periodically` follows.
                if _consecutive_failures == _GIVE_UP_AFTER:
                    log.warning(
                        "update check: giving up after %d MusicBrainz failures in a row; "
                        "backing off until one succeeds",
                        _consecutive_failures,
                    )
                else:
                    log.debug(
                        "update check: still backing off after %d failures in a row",
                        _consecutive_failures,
                    )
                gave_up = True
                break
            continue
        if _consecutive_failures:
            log.info(
                "update check: MusicBrainz answered again after %d failure(s) in a row",
                _consecutive_failures,
            )
        _consecutive_failures = 0
        # URL-only edits can settle contributions without changing any tag.
        for album in group:
            contributions.observe(album, release, mb_cache.fetched_at(str(release["id"])))
        _browse_siblings(release, group)
        if before is not None and _same_release(before, release):
            continue  # MusicBrainz has said nothing new; read no files
        examined += len(group)
        for album in group:
            # Judged BEFORE the refresh, because the refresh overwrites the
            # fields it reads. The digest is a statement about the difference
            # between the two verdicts, and this is the only moment both are
            # knowable.
            already = _already_outstanding(album, before)
            refresh_flag(album, release)
            if album.update_available:
                flagged += 1
                if not already and _from_musicbrainz(album.update_significance):
                    newly_flagged.append(album)
        if time.monotonic() - reported >= _PROGRESS_EVERY.total_seconds():
            reported = time.monotonic()
            log.info("update check: %d asked, %d with something new", asked, examined)
    result = PassResult(
        asked=asked,
        examined=examined,
        flagged=flagged,
        newly_flagged=tuple(newly_flagged),
        gone=gone,
        failed=failed,
        gave_up=gave_up,
    )
    _record_digest(result)
    # INFO only when the tick has something to report (#349). At an hourly tick
    # asking about a hundred albums this line was always worth reading; at one
    # every ten minutes asking about two it is 144 lines a day saying nothing
    # happened, which is how a log stops being read. A tick that found something
    # — a payload that moved, a release gone, a fetch that failed — still says
    # so; the digest above is what turns the rest into something a user sees.
    notable = bool(result.examined or result.gone or result.failed or result.gave_up)
    log.log(
        logging.INFO if notable else logging.DEBUG,
        "update check: %s — asked about %d release(s), %d had moved, %d album(s) "
        "have an update, %d gone from MusicBrainz, %d fetch(es) failed",
        "gave up early" if result.gave_up else "done",
        result.asked,
        result.examined,
        result.flagged,
        result.gone,
        result.failed,
    )
    return result


def _already_outstanding(album: Album, before: Release | None) -> bool:
    """Whether `album` already had an update against `before`, the payload
    MusicBrainz gave us last time. An update found now is news only if not.

    The in-memory flag answers this only when it was computed against `before`,
    which `mb_version` says. It was not after a restart the warm-up has not
    reached yet, nor after a rescan rebuilt the album, and in both cases it reads
    False. Trusting it anyway re-announced updates that had been waiting all
    along (#600). Then the old verdict is taken from `before` directly, at the
    cost of one more read of the files. That happens only for albums whose
    payload moved and whose flag nobody has computed, so it is rare.

    **No baseline counts as outstanding.** With nothing stored, nothing says
    MusicBrainz moved: a first look at a release (a new install, or a cache
    orphaned by an include change, #599) may find an update that has been
    waiting for years. The flag still goes up; it just is not announced.

    Nor does a verdict nobody could reach. If the files cannot be read,
    `refresh_flag` logs it as they are read again straight after, and the flag
    stays as it was, so there is nothing to announce either way.

    **A Settings-only update was not outstanding** (#685). It was never
    announced, so counting it here would leave a real edit from MusicBrainz
    unannounced for as long as the album waited on the user's setting.
    """
    if before is None:
        return True
    version = release_version(before)
    if version is not None and album.mb_version == version:
        return _from_musicbrainz(album.update_significance)
    try:
        return _from_musicbrainz(verdict_for(plan_for(album, before)))
    except tagger.TagMismatchError:
        return True  # a tracklist that did not fit then is outstanding too
    except formats.READ_ERRORS:
        return True  # reported by `refresh_flag`, which reads the same files next


def _from_musicbrainz(verdict: owned.Significance | str | None) -> bool:
    """Whether an album's verdict is an update MusicBrainz is offering — any
    level but `SETTINGS`, whose content the user chose a moment ago on the
    Settings page and so is never news to them (#685). The verdict is the
    album's highest-ranked change, so anything above `SETTINGS` means at least
    one change came from MusicBrainz."""
    return verdict is not None and verdict != owned.Significance.SETTINGS


def _record_digest(result: PassResult) -> None:
    """Tell the Activity feed which albums the pass found an update for (#274).

    **One entry per album, up to `NAMED_UPDATES_MAX`** (#600), each linked to
    its album. #274 wrote a single counted line on the assumption that a pass
    would find many. At #349's pace it finds one or two, and a bare count left
    the user to spot the new tile in the Update available filter. Past the cap it
    falls back to one counted line, so a burst still cannot bury the rest of the
    feed.

    **Only the transition.** `newly_flagged`, not `flagged`: an album whose
    update the user has yet to take is outstanding on every pass, so counting
    the standing state would re-announce it whenever MusicBrainz touched the
    release at all — the same "narrating a state rather than a change" #272 took
    out of the tagger.

    **From the Activity level, not the Inbox's** (#1024). Activity is a record
    the user reads when they choose to, and the Inbox is what needs them, so
    an update left in the Library is recorded too: the flag is derived and
    never stored, so nothing else says when it arrived. #273 recorded only what
    the Inbox took, treating the feed as a notification the user had declined.
    One in the apply range is still left out: it is about to be written, and
    the write records itself.

    **Silence is a valid digest**, and the common one. Nothing else the pass can
    report belongs to the user:

    * a release MusicBrainz no longer has is a standing fact, not tonight's news
      — it is still gone next week, and the album's own page has said so since
      #194/#210;
    * a failed fetch is a MusicBrainz wobble the next pass retries, kept out of
      the feed on purpose (`_diagnostic`);
    * a pass that gave up already posts its own warning, once per episode.

    So the digest speaks exactly when a person has something new to look at. No
    action scope: nothing was changed, so there is nothing to correlate or
    revert. The album id is safe to read here because the pass writes nothing
    that could move it.
    """
    routing = _routing_now()
    albums = [
        a
        for a in result.newly_flagged
        if a.update_significance is not None
        and _recorded(owned.Significance(a.update_significance), routing)
    ]
    if len(albums) > NAMED_UPDATES_MAX:
        # No pointer to where they wait: some may be in the Inbox and some in
        # the Library, and each album's own entry would say which.
        activity.record(f"Update check: {len(albums)} albums now have an update available")
        return
    for album in albums:
        activity.record(
            f"Update available from MusicBrainz ({album.update_significance})",
            album_id=album.id,
            album_label=album.label,
        )


def _due(
    albums: Sequence[Album], *, recheck_after: timedelta, now: datetime
) -> list[tuple[str, list[Album]]]:
    """The releases worth asking about, stalest first, grouped by release id.

    **Grouped**, because two albums can name the same release — a duplicate rip
    in two folders (#243), or one album split across directories that resolved
    apart. One fetch answers for all of them, and asking twice would spend a
    second rate-limited request on a payload we already have in hand.

    An album whose update the user has ignored (#271) is still due, and that is
    the point of ignoring rather than dismissing: what they are waiting for is
    MusicBrainz to move, and asking is the only way anyone finds out that it
    has.
    """
    times = mb_cache.fetch_times()
    by_release: dict[str, list[Album]] = {}
    for album in albums:
        sc = album.sidecar
        if sc is None or not sc.mb_release_id:
            continue  # nothing to ask about; not an error
        by_release.setdefault(sc.mb_release_id, []).append(album)
    due: list[tuple[datetime, str, list[Album]]] = []
    for mbid, group in by_release.items():
        last = _last_look(mbid, times)
        # A stamp in the FUTURE — a NAS clock corrected backwards by NTP — reads
        # as due rather than as fresh for however long the clock is out, so the
        # worst a bad clock can do here is cost a request. Same reading
        # `mb_cache._fresh` takes.
        if last is not None and timedelta(0) <= now - last < recheck_after:
            continue
        due.append((last or _NEVER, mbid, group))
    due.sort(key=lambda item: item[0])
    return [(mbid, group) for _, mbid, group in due]


def is_ignored(album: Album, ignored: Mapping[str, activity_store.IgnoredUpdate]) -> bool:
    """Whether the user's Ignore for `album` still holds (#271).

    It holds exactly while MusicBrainz has not moved: the stored version is what
    was on offer when they pressed Ignore, and `album.mb_version` is what is on
    offer now. An edit upstream makes those differ and the album comes back —
    which is the whole point, because the reason to ignore an update is that you
    are off to go and fix the release, and this is how you find out that
    something landed.

    Compared against the in-memory version rather than re-reading and re-hashing
    the cached payload, so a Library grid of two thousand tiles costs two
    thousand string compares rather than two thousand database reads.

    An album with no `mb_version` — never looked at, or looked at and unreadable
    — is never ignored. It is not flagged either, so there is nothing to mute.

    Nor is an album whose update is only the user's own setting arriving (#685).
    Ignore is about MusicBrainz; the way out of a Settings change is the
    setting. A bookmark can still match one — a mixed update ignored, then
    MusicBrainz's half taken by another tool — and the album page offers no box
    to untick for a Settings-only update, so honouring it would hide the change
    with no way back.
    """
    entry = ignored.get(album.id)
    return (
        entry is not None
        and entry.release_version == album.mb_version
        and album.update_significance != owned.Significance.SETTINGS
    )


def reviews(significance: owned.Significance, review_from: ReviewFrom) -> bool:
    """Whether an update reaching `significance` waits in the Inbox (#273).

    The chosen level and everything ranked above it, by `owned.ORDER`.
    """
    return review_from != "none" and owned.ranked(significance) >= owned.ranked(review_from)


def applies(significance: owned.Significance, apply_up_to: ReviewFrom) -> bool:
    """Whether an update reaching `significance` is written without asking (#273).

    The chosen level and everything ranked below it, never Structure: the config
    refuses it as a threshold, and this refuses it as a level, so a Structure
    update is never applied whatever the threshold says.
    """
    return (
        apply_up_to != "none"
        and significance != owned.Significance.STRUCTURE
        and owned.ranked(significance) <= owned.ranked(apply_up_to)
    )


def records(significance: owned.Significance, activity_from: ReviewFrom) -> bool:
    """Whether a newly found update reaching `significance` is recorded in the
    Activity feed (#1024).

    The chosen level and everything ranked above it, as for the Inbox, and
    never `SETTINGS`: the config refuses it as a threshold, and this refuses
    it as a level, because the user caused those updates on the Settings page.
    """
    return (
        activity_from != "none"
        and significance != owned.Significance.SETTINGS
        and owned.ranked(significance) >= owned.ranked(activity_from)
    )


def outcome(significance: owned.Significance, routing: GardenerConfig) -> str:
    """Where an update reaching `significance` goes: "apply", "review" or
    "library". What the Settings grid ticks for each level, from the same rules
    the pass and the Inbox follow, so the two cannot describe different things."""
    if applies(significance, routing.apply_up_to):
        return "apply"
    if reviews(significance, routing.review_from):
        return "review"
    return "library"


def _recorded(significance: owned.Significance, routing: GardenerConfig) -> bool:
    """Whether a newly found update is recorded in the Activity feed (#1024):
    the Activity level takes it and it is not about to be applied, since the
    write records itself."""
    return records(significance, routing.activity_from) and not applies(
        significance, routing.apply_up_to
    )


def waiting_at(
    album: Album, ignored: Mapping[str, activity_store.IgnoredUpdate]
) -> owned.Significance | None:
    """How far the update of a Library album reaches, if it has one the user
    has not ignored.

    Library albums only: one still in an Inbox state is listed there under that
    state, and its update is not the next thing to do about it.
    """
    if (
        album.state not in _LIBRARY_STATES
        or not album.update_available
        or album.update_significance is None
        or is_ignored(album, ignored)
    ):
        return None
    return owned.Significance(album.update_significance)


def for_review(
    albums: Sequence[Album],
    ignored: Mapping[str, activity_store.IgnoredUpdate],
    routing: GardenerConfig,
    held: Callable[[Album], bool] = lambda _: False,
) -> list[Album]:
    """The albums whose update waits in the Inbox (#273).

    Those reaching the review level, less those about to be applied, plus those
    in the apply range that `held` says could not be: the Inbox is where an
    update Harmonist was trusted with and could not write turns up.
    """
    routed = []
    for album in albums:
        level = waiting_at(album, ignored)
        if level is None:
            continue
        if applies(level, routing.apply_up_to):
            if held(album):
                routed.append(album)
        elif reviews(level, routing.review_from):
            routed.append(album)
    return routed


def waiting_by_level(
    albums: Sequence[Album], ignored: Mapping[str, activity_store.IgnoredUpdate]
) -> dict[owned.Significance, int]:
    """How many Library albums wait at each level, every level present.

    What the Settings page counts beside each level, so a threshold is chosen
    knowing how many albums it moves into or out of the Inbox.
    """
    counts = dict.fromkeys(owned.ORDER, 0)
    for album in albums:
        if (level := waiting_at(album, ignored)) is not None:
            counts[level] += 1
    return counts


def _last_look(mbid: str, times: dict[str, datetime]) -> datetime | None:
    """When this release was last looked at, by either measure, or None if it
    never has been. See `_asked` for why there are two."""
    seen = [t for t in (times.get(mbid), _asked.get(mbid)) if t is not None]
    return max(seen) if seen else None


def _browse_siblings(release: Release, group: list[Album]) -> None:
    """Browse the release's group when an album on it needs sibling evidence (#618).

    Only when needed: an album with a finding, since neither Library filter
    places one until a complete browse of its group exists. A release the user
    explicitly confirmed needs it only while that browse is missing or incomplete — its
    mismatch evidence no longer matters, only whether the group was compared.
    A provisional dismissal keeps looking for new candidate evidence (#779).
    Incomplete browses are retried, since MusicBrainz fills in unspecified
    media. At most one request per release this pass already fetched, so the
    pass's pacing bounds it. A failure keeps the previous browse and costs
    nothing but the log line.
    """
    wanted = False
    for album in group:
        assessment = contributions.assess(album)
        siblings = assessment.siblings
        settled = assessment.confirmed and siblings is not None and siblings.complete
        if assessment.eligible and assessment.has_findings and not settled:
            wanted = True
            break
    group_id = (release.get("release-group") or {}).get("id")
    if not wanted or not group_id:
        return
    try:
        releases, total = mb_cache.fetch_release_group_editions(str(group_id))
    except mb_lookup.MBError:
        log.warning(
            "update check: could not browse release group %s",
            group_id,
            exc_info=True,
            extra={"_diagnostic": True},
        )
        return
    for album in group:
        contributions.observe_group(album, releases, total)


def _same_release(before: Release, after: Release) -> bool:
    """Whether MusicBrainz has new tag-relevant data.

    URL relationships are excluded: contributions have already consumed those
    observations before this early exit, and URL edits cannot change a tag.

    Compared as canonical JSON rather than as dicts, because the question is
    whether any tag-relevant part of the stored payload changed —
    and it is immune to the one way a dict comparison here can lie. A payload
    that has round-tripped through `json.dumps` has had any tuple flattened to a
    list, so a fresh payload carrying one would compare unequal to its own
    stored copy forever. Nothing would break: the pass would simply read every
    album's files every night for no reason, and no assertion about the *answer*
    would ever notice.

    An unserialisable payload answers "not the same", which sends the album to
    the plan — the direction that costs file reads rather than the one that
    silently skips an album that might have an update outstanding.
    """
    try:
        return _canonical(_tag_payload(before)) == _canonical(_tag_payload(after))
    except TypeError, ValueError:
        log.warning(
            "update check: could not compare release payloads; reading the files instead",
            exc_info=True,
            extra={"_diagnostic": True},
        )
        return False


def _canonical(release: Release) -> str:
    """`release` as the exact string `activity_store.store_release` would write.

    `sort_keys=True` is what makes a stored payload compare equal to the fresh
    fetch it came from, so it is borrowed from the store rather than chosen
    here. Raises `TypeError` / `ValueError` on a payload that will not
    serialise; both callers below decide for themselves what that means.
    """
    return json.dumps(release, sort_keys=True)


def _tag_payload(release: Release) -> Release:
    """URL relationships do not change tags or unmute an ignored tag update."""
    return {key: value for key, value in release.items() if key != "url-relation-list"}


def release_version(release: Release) -> str | None:
    """A short, stable name for the tag-relevant release payload.

    URL relationships belong to contribution checks and cannot unmute an
    ignored tag update. All other fields retain the existing version contract.

    What a finding is judged against (#271), and the whole of how suppression
    stays honest: a dismissal remembers the version the user declined, so it
    lapses precisely when MusicBrainz moves the release and not before, and an
    open finding whose cached payload no longer matches its version is
    detectably stale rather than silently describing a change nobody is looking
    at any more.

    A content hash rather than anything MusicBrainz supplies, because it supplies
    nothing that would do: a release has no version counter, and its edit history
    is a separate request per release — the one thing this feature cannot spend.

    Truncated to 16 hex characters. This names one release's own payloads across
    a handful of versions, not a global namespace, so 64 bits is far past enough
    and the untruncated digest would only make the row harder to read in a
    `sqlite3` shell.

    None when the payload will not serialise, which means it cannot be named —
    and a finding that cannot say what it was judged against is worse than no
    finding, since it could never be retired or re-raised correctly.
    """
    try:
        return hashlib.sha256(_canonical(_tag_payload(release)).encode()).hexdigest()[:16]
    except TypeError, ValueError:
        log.warning(
            "update check: could not name this version of a release payload",
            exc_info=True,
            extra={"_diagnostic": True},
        )
        return None
