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

## Nothing here is persisted

No sidecar field, no table, no lifecycle. The flag is a hint that can be rebuilt
from durable state at any time (`warm_from_cache`), so there is nothing to
migrate, nothing to dismiss, and nothing for review-gate item 3 to object to.
The album's state stays derived.

A finding that a human has to *act* on is a different object with a different
lifetime, and it lives in `activity.db` (#271). This is only the flag.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import album_files, formats, mb_cache, mb_lookup, tagger
from .formats import owned
from .models import Album, AlbumState, Release

log = logging.getLogger(__name__)

# What fraction of the time the warm-up is allowed to be working. The rest of
# each album's share is spent asleep, so the pass takes roughly 1/duty times as
# long as the work itself and leaves the remainder to whatever else the machine
# is doing — a request being served, Plex transcoding, the library scan.
#
# A ratio rather than a fixed pause (#299). The original rested 50 ms between
# albums, which is most of the time on a laptop and a rounding error on a NAS —
# backwards, since the slow machine is the one that most needs this out of the
# way. Resting in proportion to what each album cost self-tunes to the machine
# with nothing to measure and nothing to configure.
#
# A quarter is deliberately unambitious. The warm-up's only job is that the
# Library's Update available filter is not empty after a restart, and being
# late to that is invisible — the filter under-reports by design and browsing
# fills it in. Being slow is free here; being in the way is not.
WARM_DUTY = 0.25

# How often a running pass says where it has got to. Long enough not to fill the
# log on a healthy install, short enough that a stalled pass is obvious.
_PROGRESS_EVERY = timedelta(seconds=30)

# The longest single rest, so one pathological album cannot park the pass.
_MAX_REST = timedelta(seconds=2)


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
    return tagger.plan_album(
        album.path,
        release,
        cover_path=None,
        # Same reading the re-tag button makes (`web/main.py:3829`), so the
        # flag never promises an update that the button would refuse to apply.
        # Without it every INCOMPLETE album raises `TagMismatchError` and flags
        # permanently — a defect the Incomplete filter already gathers, and not
        # an update MusicBrainz is offering.
        incomplete=album.state == AlbumState.INCOMPLETE,
        # An album can span several directories (#197); planning over the
        # primary one alone would miss whatever the other discs need.
        files=album_files.for_paths(album.folders),
    )


def refresh_flag(album: Album, release: Release) -> tagger.AlbumPlan | None:
    """Set `album.update_available` from `release`; return the plan behind it.

    The tolerant wrapper every caller uses. A file that cannot be read leaves
    the flag **as it was** rather than clearing it: the album page's comparison
    and the warm-up both run against libraries on network mounts, and a blinking
    mount must not quietly empty the filter. Under-reporting is the direction
    this feature fails in by design (see `Album.update_available`).

    **A None is not "nothing to take".** It means no plan could be built — the
    files could not be read, or the release no longer fits them — and in the
    second of those the flag is set to True. Read the verdict off
    `album.update_available`; the return value is only for callers that want to
    *show* the difference, and there is nothing to show in either case. (The
    structural case is not left unexplained: the album page reports it through
    `_shape_mismatch` and `absent_media`, which say far more about it than a
    field diff could.)

    Mutating the Album in place is what makes the flag visible to the Library:
    `ScanRunner.albums()` hands out the live snapshot, so this is the same
    object the grid will render.
    """
    try:
        plan = plan_for(album, release)
    except tagger.TagMismatchError:
        album.update_available = True
        return None
    except formats.READ_ERRORS:
        # Loud, per the unattended rule: at 3am the log is the only channel.
        # ERROR rather than WARNING — nothing recovered, we simply cannot say
        # whether this album has an update, and it will keep reporting whatever
        # it last said until something can read it.
        log.exception("could not tell whether %s has a tag update available", album.path)
        return None
    # Not `bool(plan.changes)` since #337. A change that only ADDS a credit list
    # holding one name is one to make while tagging anyway and not a reason to
    # tag: `album_artist` beside it already says the same thing, and a player
    # without the list falls back to the phrase. `albumartists` is new in Picard
    # too, so no existing library carries it — left counted, one field put every
    # album in a real library into the Inbox.
    #
    # Filtered HERE rather than in `owned.diff`, and that is the whole of the
    # design: `plan.changes` is also what the activity record and the undo are
    # built from, so a re-tag that fills the tag in must still say that it did.
    # The album page keeps showing it as a pending change for the same reason —
    # the page says what a re-tag would do, this flag says whether you need one.
    album.update_available = any(
        not owned.is_opportunistic(field, before, after)
        for changes in plan.changes.values()
        for field, (before, after) in changes.items()
    )
    return plan


def warm_from_cache(albums: Sequence[Album], *, duty: float = WARM_DUTY) -> int:
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

    **Paced by duty cycle, and audible while it runs** (#299). It shipped doing
    neither, and the two failures compounded: on a NAS it took the CPU for
    however long a whole library takes to parse, while saying nothing at all
    until it finished — so an album page that crawled during that window looked
    identical to one that had hung, with no way to tell which from the log.
    """
    started = time.monotonic()
    log.info("update-available warm-up: checking %d albums", len(albums))
    flagged = 0
    looked = 0
    reported = started
    for album in albums:
        sc = album.sidecar
        if sc is None or not sc.mb_release_id:
            continue  # nothing to compare against; not an error
        release = mb_cache.stored_release(sc.mb_release_id)
        if release is None:
            continue  # never fetched, or fetched under a different `inc`
        looked += 1
        at = time.monotonic()
        refresh_flag(album, release)
        # The verdict is the flag, not the return value — a None comes back both
        # for an unreadable album (not flagged) and for one whose tracklist no
        # longer fits (flagged), so counting the return would undercount.
        if album.update_available:
            flagged += 1
        _rest(time.monotonic() - at, duty)
        if time.monotonic() - reported >= _PROGRESS_EVERY.total_seconds():
            reported = time.monotonic()
            log.info(
                "update-available warm-up: %d albums looked at, %d with an update", looked, flagged
            )
    log.info(
        "update-available warm-up: done in %.0fs — %d of %d albums with a stored release "
        "have an update",
        time.monotonic() - started,
        flagged,
        looked,
    )
    return flagged


def _rest(worked: float, duty: float) -> None:
    """Sleep long enough that the pass only ever uses `duty` of the machine.

    A fixed pause was the original, and it is the wrong shape: 50 ms between
    albums is most of the time on a laptop and a rounding error on a NAS, which
    is exactly backwards — the slow machine is the one that needs the pass to
    get out of the way. Resting in proportion to what each album actually cost
    self-tunes with no measurement, no configuration and nothing shared with the
    request path to coordinate through.

    Capped, so one pathological album — a hundred tracks on a failing disk —
    cannot stall the whole pass behind a single long sleep.
    """
    if duty <= 0 or duty >= 1:
        return  # 0 disables pacing (tests); 1 means "no rest", same thing here
    time.sleep(min(worked * (1 / duty - 1), _MAX_REST.total_seconds()))


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
    sets `album.update_available`, and writes nothing to anybody's files. That
    is not a stage of a plan, it is what the classifier currently permits —
    `owned.AUTO_APPLY` is empty, so every change needs a person, and until #271
    gives a finding somewhere to live there is nothing for this to hand one to.
    Two things fall out of that: the pass cannot damage a library, and its whole
    value is that the Library's Update available filter stops depending on
    somebody having happened to open the album (#287, #293).

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
        if before is not None and _same_release(before, release):
            continue  # MusicBrainz has said nothing new; read no files
        examined += len(group)
        for album in group:
            refresh_flag(album, release)
            if album.update_available:
                flagged += 1
        if time.monotonic() - reported >= _PROGRESS_EVERY.total_seconds():
            reported = time.monotonic()
            log.info("update check: %d asked, %d with something new", asked, examined)
    result = PassResult(
        asked=asked, examined=examined, flagged=flagged, gone=gone, failed=failed, gave_up=gave_up
    )
    # INFO only when the tick has something to report (#349). At an hourly tick
    # asking about a hundred albums this line was always worth reading; at one
    # every ten minutes asking about two it is 144 lines a day saying nothing
    # happened, which is how a log stops being read. A tick that found something
    # — a payload that moved, a release gone, a fetch that failed — still says
    # so; #274's digest is what turns the rest into something a user sees.
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


def _due(
    albums: Sequence[Album], *, recheck_after: timedelta, now: datetime
) -> list[tuple[str, list[Album]]]:
    """The releases worth asking about, stalest first, grouped by release id.

    **Grouped**, because two albums can name the same release — a duplicate rip
    in two folders (#243), or one album split across directories that resolved
    apart. One fetch answers for all of them, and asking twice would spend a
    second rate-limited request on a payload we already have in hand.
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


def _last_look(mbid: str, times: dict[str, datetime]) -> datetime | None:
    """When this release was last looked at, by either measure, or None if it
    never has been. See `_asked` for why there are two."""
    seen = [t for t in (times.get(mbid), _asked.get(mbid)) if t is not None]
    return max(seen) if seen else None


def _same_release(before: Release, after: Release) -> bool:
    """Whether MusicBrainz has anything to say that it had not already said.

    Compared as the JSON the store would write rather than as dicts, because
    that is precisely the question — *would re-storing this change the row?* —
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
        return json.dumps(before, sort_keys=True) == json.dumps(after, sort_keys=True)
    except (TypeError, ValueError):
        log.warning(
            "update check: could not compare release payloads; reading the files instead",
            exc_info=True,
            extra={"_diagnostic": True},
        )
        return False
