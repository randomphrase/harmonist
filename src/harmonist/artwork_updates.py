"""Library artwork findings and paced, review-only CAA discovery (#269, #276).

Scans retain descriptions, never image bytes. The shared artwork plan judges
those descriptions against the archive observation in memory. Filtering is pure;
only warming and checking read the durable cache, and only checking uses CAA.

Where a finding waits is routed as a tag update's is (#852): an album whose plan
does at least what the user's threshold names waits in the Inbox, and the rest
are found through the Library's Artwork updates filter. Nothing about it is
stored: it is a function of the same plan, rebuilt whenever it is asked.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import (
    activity,
    activity_store,
    artwork,
    artwork_watch,
    caa_cache,
    cover_art,
    derived,
    formats,
    mb_cache,
    mb_lookup,
    progress,
)
from .config import ArtworkReviewFrom, GardenerConfig
from .models import Album, AlbumState

log = logging.getLogger(__name__)
TICK = timedelta(minutes=10)
_WINDOW = timedelta(days=1)
_RETRY = timedelta(hours=1)
#: Albums an artwork update can be routed for, as for a tag update: one still in
#: an Inbox state is listed there under its state, which is the next thing to do.
_LIBRARY_STATES = frozenset({AlbumState.COMPLETE, AlbumState.INCOMPLETE})
#: Operations in the order a threshold ranks them, the least an update can do
#: first: an Inbox taking additions takes replacements too.
ORDER: tuple[artwork.Operation, ...] = (artwork.Operation.ADDITION, artwork.Operation.REPLACEMENT)
#: How many albums the pass's digest names one by one, as the tag digest does
#: (#600); past it, one counted line, so a burst cannot bury the feed.
_NAMED_MAX = 5


@dataclass
class _Failures:
    """Worker-local pacing, independent of per-release observations.

    Kept out of Activity (#864): the check changes nothing, retries on its own
    and never gives up, and an archive outage is not the user's to act on, as
    for the album page's own check (`_check_cover_art`). Nothing here is an
    archive answer, and restarting permits another attempt.
    """

    delay: float = 0
    retry_after: float = 0
    count: int = 0

    def failed(self) -> None:
        self.delay = min(max(TICK.total_seconds(), self.delay * 2), _RETRY.total_seconds())
        self.retry_after = time.monotonic() + self.delay
        self.count += 1

    def succeeded(self) -> None:
        if self.count:
            # This one check is evidence of progress, not proof that every
            # release endpoint has recovered.
            log.info("background artwork check succeeded after %d failed checks", self.count)
        self.delay = 0
        self.retry_after = 0
        self.count = 0


@dataclass(frozen=True)
class LocalArtwork:
    """What an album's files carry, for the plan and its download threshold.

    No field has a default, on purpose: the page once built one of these
    without the audio facts, and its threshold downloaded nothing for an album
    the plan could embiggen (#872). mypy now names every construction that
    forgets one."""

    tracks: tuple[tuple[Path, formats.EmbeddedArt | None], ...]
    cover: artwork.FolderCover | None
    unreadable: bool
    #: Every audio file is lossless (`formats.all_lossless`), which lets the
    #: plan embiggen the tracks (#872).
    lossless: bool
    #: The audio files' combined size, from the scan's stat: what lossy
    #: embiggening's growth is measured against (#872).
    audio_bytes: int | None

    @property
    def missing(self) -> bool:
        return not self.unreadable and self.cover is None and not any(a for _, a in self.tracks)

    @property
    def missing_tracks(self) -> bool:
        return not self.unreadable and any(a is None for _, a in self.tracks)

    @property
    def different_tracks(self) -> bool:
        return not self.unreadable and artwork.has_per_track_art(
            a.digest if a else None for _, a in self.tracks
        )

    @property
    def oversize(self) -> bool:
        """Any image a player won't read (#889). Each track's image and the
        folder cover are separate files to a player, so one is enough. Not
        gated on `unreadable` as the others are: a file that can't be read
        hides nothing about an image that was."""
        images = [a for _, a in self.tracks] + [self.cover.image if self.cover else None]
        return any(a is not None and artwork.too_large(a) for a in images)


@dataclass(frozen=True)
class Observation:
    answer: activity_store.CachedCoverArt | None
    image: formats.EmbeddedArt | None


_observed: dict[str, Observation] = {}
_attempted: dict[str, datetime] = {}
_failures = _Failures()
_revision = 0
_policy: Callable[[], artwork.FolderCoverPolicy] = lambda: artwork.FolderCoverPolicy.NEVER
_routing: Callable[[], GardenerConfig] = GardenerConfig


def configure(
    policy: Callable[[], artwork.FolderCoverPolicy],
    routing: Callable[[], GardenerConfig] = GardenerConfig,
) -> None:
    """Read the folder-cover policy and the routing thresholds through these,
    so a Settings save is live on the next check without reconfiguring."""
    global _policy, _routing, _revision, _failures
    _policy = policy
    _routing = routing
    _observed.clear()
    _attempted.clear()
    _failures = _Failures()
    _revision = 0
    artwork_watch.reset()


def revision() -> int:
    """A changed candidate refreshes an open Library through its status poll."""
    return _revision


def read_local(
    files: Sequence[Path],
    fields: Sequence[formats.ScanFields],
    cover_path: Path | None,
    audio_bytes: int | None,
) -> LocalArtwork:
    """Finish a scan's observation with one folder-image read on a cache miss."""
    unreadable = any(f.unreadable or (f.has_cover and f.art is None) for f in fields)
    cover = None
    if cover_path is not None:
        try:
            data = cover_path.read_bytes()
        except OSError:
            log.exception("could not assess artwork in %s", cover_path)
            unreadable = True
        else:
            cover = artwork.FolderCover(
                name=cover_path.name,
                path=cover_path,
                image=formats.EmbeddedArt.of(
                    data, "image/png" if cover_path.suffix.lower() == ".png" else "image/jpeg"
                ),
                beside=cover_art.covers_beside(cover_path),
            )
    return LocalArtwork(
        tuple((p, f.art) for p, f in zip(files, fields, strict=True) if not f.unreadable),
        cover,
        unreadable,
        formats.all_lossless(f.codec for f in fields),
        audio_bytes,
    )


def observe(mbid: str) -> None:
    """Publish one durable CAA answer and its cached original; no network."""
    answer = caa_cache.stored(mbid)
    front = cover_art.cached_front(mbid)
    global _revision
    observation = Observation(
        answer,
        formats.EmbeddedArt.of(front.data, front.mime) if front is not None else None,
    )
    previous = _observed.get(mbid)
    _observed[mbid] = observation
    watch_changed = artwork_watch.observe(mbid)
    if (
        watch_changed
        or (previous.image if previous else None) != observation.image
        or (
            bool(previous and previous.answer and previous.answer.from_release_group)
            != bool(answer and answer.from_release_group)
        )
        or (previous.answer.replaced if previous and previous.answer else None)
        != (answer.replaced if answer else None)
    ):
        _revision += 1


def warm(albums: Sequence[Album]) -> None:
    """Read every release not yet observed, shown in the status bar while there
    are any (#757): the first pass after a start reads the whole library, and
    every later one is usually nothing at all."""
    unread = [
        mbid
        for mbid in dict.fromkeys(a.sidecar.mb_release_id for a in albums if a.sidecar)
        if mbid and mbid not in _observed
    ]
    if not unread:
        return
    with progress.job("Reading artwork", total=len(unread)) as job:
        for mbid in unread:
            observe(mbid)
            job.advance()


def operation(album: Album) -> artwork.Operation | None:
    """The default review's operation, independent of tag significance/ignores."""
    plan = _plan(album)
    return plan.operation(artwork.Scope.ALL) if plan is not None else None


def _plan(album: Album) -> artwork.ArtworkPlan | None:
    """The default review's plan, from the scan's descriptions and the archive
    observation in memory: no file reads and no requests. Built afresh on each
    call, which costs microseconds per album, so nothing can serve one that a
    rescan, an observation or a setting has since made stale."""
    local = album.artwork_local
    if local is None or local.unreadable:
        return None
    sc = album.sidecar
    observed = _observed.get(sc.mb_release_id) if sc and sc.mb_release_id else None
    image = observed.image if observed else None
    own = (
        observed
        and observed.answer
        and observed.answer.has_art
        and not observed.answer.from_release_group
    )
    borrowed = (
        frozenset(b.digest for b in sc.borrowed_artwork if b.release == sc.mb_release_id)
        if sc and own
        else frozenset()
    )
    choices, picks = artwork_watch.proposal(sc, local.tracks, local.cover, _policy())
    return artwork.plan(
        album.path,
        local.tracks,
        local.cover,
        image,
        # A replaced front gives way to whichever front the archive has now,
        # the group's included: unlike a stand-in, it was never this release's
        # to wait for (#886).
        stand_ins=borrowed | _superseded(album),
        choices=choices,
        picks=picks,
        excluded_archive=artwork_watch.excluded(sc, image),
        folder_cover=_policy(),
        lossless=local.lossless,
        audio_bytes=local.audio_bytes,
        # A copy squished to fit holds its original (#888).
        originals=derived.originals(),
    )


def _superseded(album: Album) -> frozenset[str]:
    """What the album carries of a front the archive has since replaced (#886),
    from the observation in memory."""
    local = album.artwork_local
    sc = album.sidecar
    observed = _observed.get(sc.mb_release_id) if sc and sc.mb_release_id else None
    if local is None or observed is None or observed.answer is None:
        return frozenset()
    carried = [a for _, a in local.tracks if a is not None]
    if local.cover is not None:
        carried.append(local.cover.image)
    return artwork.superseded(observed.answer.replaced, carried, originals=derived.originals())


def reviews(op: artwork.Operation, review_from: ArtworkReviewFrom) -> bool:
    """Whether an artwork update doing `op` waits in the Inbox under the
    threshold `review_from` (#852): that operation and every one after it."""
    return review_from != "none" and ORDER.index(op) >= ORDER.index(review_from)


def applies(op: artwork.Operation, apply_up_to: ArtworkReviewFrom) -> bool:
    """Whether an artwork update doing `op` is written without asking (#853):
    that operation and every one before it, never a replacement. The config
    refuses replacement as a threshold, and this refuses it as an operation,
    so nothing written with nobody watching overwrites an image."""
    return (
        apply_up_to != "none"
        and op is not artwork.Operation.REPLACEMENT
        and ORDER.index(op) <= ORDER.index(apply_up_to)
    )


def outcome(op: artwork.Operation, routing: GardenerConfig) -> str:
    """Where an artwork update doing `op` goes: "apply", "review" or "library",
    from the same rules the pass and the Inbox follow, for the Settings grid."""
    if applies(op, routing.artwork_apply_up_to):
        return "apply"
    if reviews(op, routing.artwork_review_from):
        return "review"
    return "library"


def waiting(album: Album) -> artwork.Operation | None:
    """A Library album's artwork update, if it has one."""
    return operation(album) if album.state in _LIBRARY_STATES else None


def watched(album: Album) -> bool:
    return artwork_watch.active(album.sidecar) is not None


def routed(
    album: Album, routing: GardenerConfig, held: Callable[[Album], bool] = lambda _: False
) -> bool:
    """Whether this album's artwork update waits in the Inbox (#852).

    One the review threshold takes, less one about to be applied (#853), plus
    one the apply pass had to hold back: the Inbox is where an update Harmonist
    was trusted with and could not write turns up. Any update an album's
    "Better artwork wanted" watch found waits there whatever the thresholds
    say: the watch was a request to be told, and is never applied unattended.
    Without a threshold or a watch, no plan is built, so the defaults cost one
    sidecar look per album.
    """
    is_watched = watched(album)
    if (
        routing.artwork_review_from == "none"
        and routing.artwork_apply_up_to == "none"
        and not is_watched
    ):
        return False
    op = waiting(album)
    if op is None:
        return False
    if is_watched:
        return True
    if applies(op, routing.artwork_apply_up_to):
        return held(album)
    return reviews(op, routing.artwork_review_from)


def for_review(
    albums: Sequence[Album],
    routing: GardenerConfig,
    held: Callable[[Album], bool] = lambda _: False,
) -> list[Album]:
    """The albums whose artwork update waits in the Inbox, by `routed`."""
    return [a for a in albums if routed(a, routing, held)]


def due(
    albums: Sequence[Album], apply_up_to: ArtworkReviewFrom, held: Callable[[Album], bool]
) -> list[Album]:
    """Library albums whose artwork update the apply pass writes (#853): in the
    apply range, not watched, not held. Without a threshold, no plan is built."""
    if apply_up_to == "none":
        return []
    return [
        a
        for a in albums
        if not watched(a)
        and (op := waiting(a)) is not None
        and applies(op, apply_up_to)
        and not held(a)
    ]


def fingerprint(album: Album) -> str | None:
    """What the scan says this album's artwork update would write, as a key a
    held update is kept against: it lapses once the files or the archive's
    answer change the plan."""
    plan = _plan(album)
    return plan.fingerprint(artwork.Scope.ALL) if plan is not None else None


def waiting_by_operation(albums: Sequence[Album]) -> dict[artwork.Operation, int]:
    """How many Library albums have an artwork update doing each operation,
    every operation present: what the Settings page counts beside each row."""
    counts = dict.fromkeys(ORDER, 0)
    for album in albums:
        if (op := waiting(album)) is not None:
            counts[op] += 1
    return counts


@dataclass(frozen=True)
class Proposal:
    """What an album's artwork update would write, as its Inbox card says it."""

    operation: artwork.Operation
    #: The change the card shows, as the two images side by side: what one
    #: target holds now, None for an addition, and what it would hold. A
    #: replacement's when there is one, since that is what the card's group is
    #: about and what someone deciding whether they mind needs to see.
    before: formats.EmbeddedArt | None
    after: formats.EmbeddedArt
    #: How many readable tracks the album has, for "3 of 12 tracks".
    tracks_total: int
    tracks_added: int
    tracks_replaced: int
    #: What it does to the folder cover, if anything, and the file's name.
    cover: artwork.Operation | None
    cover_name: str | None
    #: The plan's fingerprint, which a selection of this card carries (#859):
    #: what the user reviewed, so nothing else is written in its name.
    fingerprint: str
    #: The name the folder cover is renamed to with its image as it is: a
    #: cover named for the other type (#907).
    cover_renamed_to: str | None = None

    @property
    def summary(self) -> str:
        """Which files it writes, short enough for one line of a grid card:
        "Updates all 12 tracks", "Updates 3 of 12 tracks", "Updates cover.jpg
        only", "Updates all 12 tracks and cover.jpg", "Renames cover.jpg to
        cover.png".

        Whether it adds or replaces is the card's group heading, and the images
        beside it say what changes; this is only where. Counted in files, the
        folder cover by name, as the album page counts them.
        """
        n = self.tracks_added + self.tracks_replaced
        total = self.tracks_total
        if not n:
            tracks = None
        elif n == total and total > 1:
            tracks = f"all {total} tracks"
        elif n == total:
            tracks = "1 track"
        else:
            tracks = f"{n} of {total} tracks"
        cover = self.cover_name if self.cover is not None else None
        if cover and self.cover_renamed_to:
            renames = f"{cover} to {self.cover_renamed_to}"
            return f"Updates {tracks} and renames {renames}" if tracks else f"Renames {renames}"
        if tracks and cover:
            return f"Updates {tracks} and {cover}"
        if cover:
            return f"Updates {cover} only"
        return f"Updates {tracks}"

    @property
    def before_facts(self) -> str | None:
        """The facts line the album page's rows give an image, such as
        "1400×1400 · JPEG · 718 KB" (`artwork.describe`)."""
        return artwork.describe(self.before) if self.before is not None else None

    @property
    def after_facts(self) -> str:
        return artwork.describe(self.after)


def proposal(album: Album) -> Proposal | None:
    """`album`'s artwork update for its Inbox card, from the same plan that
    routed it: no file reads and no requests."""
    plan = _plan(album)
    if plan is None:
        return None
    op = plan.operation(artwork.Scope.ALL)
    changes = plan.scoped(artwork.Scope.ALL)
    if op is None or not changes:
        return None
    tracks = [c for c in changes if not c.folder_cover]
    cover = next((c for c in changes if c.folder_cover), None)
    shown = next((c for c in changes if c.operation is artwork.Operation.REPLACEMENT), changes[0])
    # The plan read its before-images off these same descriptions, so a
    # replaced image is always among them.
    before = _carried(album, shown.before) if shown.before is not None else None
    return Proposal(
        operation=op,
        before=before,
        after=plan.image_for(shown).image,
        tracks_total=len(album.artwork_local.tracks) if album.artwork_local else len(tracks),
        tracks_added=sum(1 for c in tracks if c.operation is artwork.Operation.ADDITION),
        tracks_replaced=sum(1 for c in tracks if c.operation is artwork.Operation.REPLACEMENT),
        cover=cover.operation if cover is not None else None,
        cover_name=cover.target.name if cover is not None else None,
        fingerprint=plan.fingerprint(artwork.Scope.ALL),
        cover_renamed_to=cover.rename_to.name
        if cover is not None and cover.rename_only and cover.rename_to
        else None,
    )


def _carried(album: Album, digest: str) -> formats.EmbeddedArt | None:
    """The description of an image the album carries, by digest, from the scan."""
    local = album.artwork_local
    if local is None:
        return None
    carried = [a for _, a in local.tracks if a is not None]
    if local.cover is not None:
        carried.append(local.cover.image)
    return next((a for a in carried if a.digest == digest), None)


def threshold(album: Album) -> int | None:
    """Width a candidate must exceed to be useful; -1 means any image.

    A stand-in gives way regardless of dimensions, and so does a front the
    archive has since replaced (#886). Missing images can also
    take an unmeasurable candidate. Existing embedded art needs enlarging only
    on an album with no folder cover (#872): always if it's lossless, and if
    it's lossy, while that grows it little. The archive's byte size is known
    only once it's fetched, so a lossy album fetches by width like a lossless
    one, and the plan weighs the bytes.
    """
    local = album.artwork_local
    sc = album.sidecar
    borrowed = (
        frozenset(b.digest for b in sc.borrowed_artwork if b.release == sc.mb_release_id)
        if sc
        else frozenset()
    )
    return download_threshold(local, borrowed | _superseded(album))


def download_threshold(local: LocalArtwork | None, borrowed: frozenset[str]) -> int | None:
    """Shared by discovery and the album page: fetch what the plan could use."""
    if local is None:
        return -1
    if local.unreadable:
        return None
    carried = {a.digest for _, a in local.tracks if a}
    if local.cover:
        carried.add(local.cover.image.digest)
    if carried & borrowed:
        return -1
    per_track = artwork.has_per_track_art(a.digest if a else None for _, a in local.tracks)
    if local.cover is None:
        if local.missing or _policy() is artwork.FolderCoverPolicy.IF_MISSING:
            return -1
        # The one album whose tracks a larger front can embiggen (#872).
        own = next((a for _, a in local.tracks if a), None)
        if (local.lossless or local.audio_bytes) and not per_track and own and own.size:
            return own.size.width
        return None
    if per_track:
        return None
    size = local.cover.image.size
    return size.width if size else None


def _out_of_step(
    mbid: str, group: str | None, answer: activity_store.CachedCoverArt
) -> list[tuple[str, str]]:
    """The stored listings whose front is not the one the check found (#927).

    The picker offers what a listing names, and After apply what the front
    check found. Each keeps its own date, so a front the check has just found
    can be missing from a listing still fresh by its own. The release's listing
    should name the answer's front unless that came from the group; the
    group's should name it when it did, and nothing when neither has one. A
    listing never stored is not out of step: the album page lists it when the
    album is first shown.
    """
    expected = {("release", mbid): None if answer.from_release_group else answer.image_url}
    if group is not None and (answer.from_release_group or not answer.has_art):
        expected[("release-group", group)] = answer.image_url
    return [
        (kind, key)
        for (kind, key), url in expected.items()
        if (listed := caa_cache.stored_listing(kind, key)) is not None
        and next((c.image_url for c in listed if c.front), None) != url
    ]


def sweep(albums: Sequence[Album], *, limit: int | None = None) -> int:
    """Refresh a bounded oldest-first slice on CAA's own clock. Never write music.

    Attempts also pace failures and evicted images, which cannot advance the
    durable observation date. Stop at the first service failure and back off
    across releases so an outage cannot advance through the library each tick.
    MusicBrainz is read from its cache except when a watch's release-group
    identity needs refilling; its age never gates CAA work.
    """
    if time.monotonic() < _failures.retry_after:
        return 0
    now = datetime.now(UTC)
    groups: dict[str, list[Album]] = {}
    for album in albums:
        if (
            (
                album.state in {AlbumState.COMPLETE, AlbumState.INCOMPLETE}
                or artwork_watch.active(album.sidecar)
            )
            and album.sidecar
            and album.sidecar.mb_release_id
        ):
            groups.setdefault(album.sidecar.mb_release_id, []).append(album)
    floor = datetime.min.replace(tzinfo=UTC)
    due: list[tuple[datetime, str, int | None]] = []
    for mbid, copies in groups.items():
        attempted = _attempted.get(mbid)
        if attempted and timedelta(0) <= now - attempted < _RETRY:
            continue
        width = min((w for a in copies if (w := threshold(a)) is not None), default=None)
        watching = any(artwork_watch.active(a.sidecar) for a in copies)
        if caa_cache.front_due(mbid, keep_if_wider_than=width) or (
            watching and artwork_watch.due(mbid)
        ):
            known = caa_cache.stored(mbid)
            due.append((max(known.fetched_at if known else floor, attempted or floor), mbid, width))
    due.sort()
    count = limit if limit is not None else math.ceil(len(due) * TICK / _WINDOW)
    deadline = time.monotonic() + TICK.total_seconds() / 2
    checked = 0
    # Not counting held albums: one the apply pass is about to write is not
    # news, and one it held was already waiting in the Inbox.
    routing = _routing()
    found: list[tuple[Album, artwork.Operation]] = []
    for _, mbid, width in due[:count]:
        if time.monotonic() >= deadline:
            break
        _attempted[mbid] = datetime.now(UTC)
        # Judged before anything this check learns reaches the plan, so the
        # digest names only albums the check itself sent to the Inbox.
        waited = {id(a) for a in groups[mbid] if routed(a, routing)}
        release = mb_cache.stored_release(mbid)
        group = (release.get("release-group") or {}).get("id") if release else None
        try:
            if any(artwork_watch.active(a.sidecar) for a in groups[mbid]):
                artwork_watch.check(mbid)
                artwork_watch.prepare(groups[mbid])
            front = caa_cache.front(
                mbid,
                release_group_mbid=group if isinstance(group, str) else None,
                keep_if_wider_than=width,
            )
            # The picker beside the plan this check changes must offer what
            # it found, so a listing behind it is asked again now (#927).
            for kind, key in _out_of_step(mbid, group if isinstance(group, str) else None, front):
                caa_cache.listing(kind, key, max_age=caa_cache.FRESH)
            # The check was sized by what the albums carried before it learnt
            # one of those images is a front the archive has replaced (#886).
            # Its successor wins whatever it measures, so it is fetched now,
            # one image, rather than a whole check later.
            observe(mbid)
            wanted = min((w for a in groups[mbid] if (w := threshold(a)) is not None), default=None)
            answer = caa_cache.stored(mbid)
            if (
                wanted != width
                and answer is not None
                and (url := cover_art.unfetched_winner(mbid, answer, wanted))
            ):
                cover_art.fetch_image(mbid, url)
        except cover_art.CoverArtError, mb_lookup.MBError:
            _failures.failed()
            # Kept out of the Activity feed (`_diagnostic`, see `_Failures`);
            # the log keeps every attempt's traceback.
            log.warning(
                "background artwork check failed for %s; next attempt in at least %d minutes",
                mbid,
                _failures.delay // 60,
                exc_info=True,
                extra={"_diagnostic": True},
            )
            break
        observe(mbid)
        _failures.succeeded()
        checked += 1
        found.extend(
            (a, op)
            for a in groups[mbid]
            if id(a) not in waited and routed(a, routing) and (op := waiting(a)) is not None
        )
    _record_digest(found)
    return checked


def _record_digest(albums: Sequence[tuple[Album, artwork.Operation]]) -> None:
    """Tell the Activity feed which albums this check sent to the Inbox (#852),
    as the tag update check does (#274, #600).

    **Only the transition**: an album whose update was already waiting is not
    news when the archive is asked about it again. **Only the Inbox**: an update
    left to the Library filter is one the user chose not to be told about. One
    the startup warm-up rebuilds is not announced either, since it is not new.

    No action scope: nothing was changed, so there is nothing to correlate or
    revert, and the album ids are safe to read because the check writes
    nothing that could move them.
    """
    if len(albums) > _NAMED_MAX:
        activity.record(
            f"Artwork check: {len(albums)} albums now have an artwork update — see the Inbox"
        )
        return
    for album, op in albums:
        activity.record(
            f"Artwork update available from the Cover Art Archive ({op.value})",
            album_id=album.id,
            album_label=album.label,
        )
