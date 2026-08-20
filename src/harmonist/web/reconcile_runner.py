"""Background runner for per-album reconciliation.

Mirror of `SyncRunner`'s shape: single-flight, thread-based, status pollable.
When the scanner sees Orphan albums, `/tasks` kicks this runner (subject to a
small debounce so back-to-back polls don't spawn redundant work).

The runner iterates orphans, calls `reconcile.reconcile_album()` for each,
rate-limits MB queries at ~1/sec to stay within MusicBrainz's published
limits.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harmonist import activity, activity_store, live_counts, sidecar

if TYPE_CHECKING:
    from harmonist.models import Album

log = logging.getLogger(__name__)


# Seconds between MB lookups during reconciliation. MusicBrainz documents a
# 1 req/sec limit; we err on the safe side.
MB_RATE_LIMIT_SECONDS = 1.0

# Seconds we must wait after a run completes before kicking another. Prevents
# /tasks polls (every 1.5s during sync) from spinning up redundant runs when
# all the orphans are already non-reconcilable (no MBID atom).
RERUN_DEBOUNCE_SECONDS = 5.0


@dataclass
class ReconcileStatus:
    state: str = "idle"  # "idle" | "running"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    current_item: str = ""
    completed: int = 0
    total: int = 0
    last_error: str | None = None
    # Live inbox/library counts DURING a pass: the already-sidecar'd base
    # (captured at start, excluding the orphans being reconciled) plus the
    # running outcome tallies. An un-reconciled orphan isn't in any count yet;
    # reconcile is what files it into one. Lets the UI show the counts move
    # without a mid-pass rescan. Only meaningful while state == "running".
    inbox: int = 0
    library: int = 0
    new: int = 0
    needs_sync: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "current_item": self.current_item,
            "completed": self.completed,
            "total": self.total,
            "last_error": self.last_error,
            "inbox": self.inbox,
            "library": self.library,
            "new": self.new,
            "needs_sync": self.needs_sync,
        }


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class ReconcileRunner:
    """Owns a single background reconciliation job at a time."""

    def __init__(self, runner_fn: Callable[..., None]):
        """`runner_fn(status_updater)` is the callable that iterates orphans
        and reconciles them. The runner injects a status updater so the
        function can report progress (current_item, completed counters).
        """
        self._runner_fn = runner_fn
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._status = ReconcileStatus()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._status.state == "running"

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._status.to_dict()

    def can_start(self) -> bool:
        """True if not already running AND last completion is far enough
        in the past that we haven't just finished a redundant pass."""
        with self._lock:
            if self._status.state == "running":
                return False
            if self._status.finished_at is None:
                return True
            since = datetime.now(UTC) - self._status.finished_at
            return since.total_seconds() >= RERUN_DEBOUNCE_SECONDS

    def start(self) -> bool:
        """Spawn the reconciliation thread. Returns True if started, False if
        the runner is already running or still inside the debounce window.
        """
        with self._lock:
            if not self._can_start_locked():
                return False
            self._status = ReconcileStatus(
                state="running",
                started_at=datetime.now(UTC),
            )
        self._thread = threading.Thread(target=self._run, daemon=True, name="harmonist-reconcile")
        self._thread.start()
        return True

    def _can_start_locked(self) -> bool:
        if self._status.state == "running":
            return False
        if self._status.finished_at is None:
            return True
        since = datetime.now(UTC) - self._status.finished_at
        return since.total_seconds() >= RERUN_DEBOUNCE_SECONDS

    def _run(self) -> None:
        error: str | None = None
        try:
            self._runner_fn(self._update_status)
        except Exception as e:
            log.exception("reconcile run failed")
            error = str(e)
        finally:
            with self._lock:
                self._status.state = "idle"
                self._status.finished_at = datetime.now(UTC)
                self._status.current_item = ""
                self._status.last_error = error

    def _update_status(
        self,
        *,
        current_item: str = "",
        completed: int | None = None,
        total: int | None = None,
        inbox: int | None = None,
        library: int | None = None,
        new: int | None = None,
        needs_sync: int | None = None,
    ) -> None:
        """Callback handed to the runner_fn so it can report progress."""
        with self._lock:
            if current_item is not None:
                self._status.current_item = current_item
            if completed is not None:
                self._status.completed = completed
            if total is not None:
                self._status.total = total
            if inbox is not None:
                self._status.inbox = inbox
            if library is not None:
                self._status.library = library
            if new is not None:
                self._status.new = new
            if needs_sync is not None:
                self._status.needs_sync = needs_sync


def reconcile_pending_orphans(
    music_dir: Path,
    *,
    fetch_urls: Callable[[str], list[str]],
    fetch_track_count: Callable[[str], int] | None = None,
    recover_url: Callable[[Path], str | None] | None = None,
    status_updater: Callable[..., None] | None = None,
    rate_limit_seconds: float = MB_RATE_LIMIT_SECONDS,
    exempt_paths: set[Path] | None = None,
    albums: list[Album] | None = None,
) -> dict[str, int]:
    """Reconcile every NEW album with an MBID atom.

    `albums` is the already-scanned library snapshot (from the background
    scanner). When given, we reuse it instead of re-walking `music_dir` — the
    scanner just finished, so a second full scan is pure wasted minutes (and a
    second copy of the snapshot in memory). Only falls back to scanning when no
    snapshot is supplied (e.g. direct callers / tests).

    `fetch_track_count` is injected, and omitting it SKIPS the track-count
    backfill rather than falling back to the real lookup. Defaulting it to
    `mb_lookup.fetch_release_track_count` reads as a convenience and behaves as
    a trap: every test that reconciles anything would start talking to
    MusicBrainz at one request per second, having asked for no such thing. The
    one caller that wants the network says so.

    Albums whose path is in `exempt_paths` are skipped. This is the
    mechanism that respects user intent after a Forget — without it, the
    auto-reconciliation would immediately re-create the sidecar the user
    just deleted. Exemption is in-memory only; server restart clears it.

    Yielded progress goes through status_updater. Returns final stats.
    """
    from harmonist import reconcile, scanner, url_recovery
    from harmonist.models import AlbumState

    recover = recover_url or url_recovery.recover_store_url
    terminal = {AlbumState.COMPLETE, AlbumState.INCOMPLETE}

    exempt = exempt_paths or set()
    if albums is None:
        # No snapshot handed in — walk the library ourselves. This can take a
        # while on a large tree, so announce it (the feed would be silent).
        # Phrased as progress, not a start: the "Reconcile started — N to check"
        # line below is the start, and two "started" entries read as a bug.
        activity.info("Reconcile: scanning the library…")
        albums = scanner.scan(music_dir)
    # NEW: derive a sidecar. TAGGING: the sidecar's MBID disagrees with the file
    # tags (an external Picard re-tag) — adopt the files. Both are reconcile's job.
    pending = [
        a
        for a in albums
        if a.state in (AlbumState.NEW, AlbumState.TAGGING) and a.path not in exempt
    ]
    total = len(pending)

    # Base counts at start, EXCLUDING the orphans we're about to reconcile — an
    # un-reconciled orphan isn't in any count yet. The live counts below are
    # base + the running outcome tallies, so the UI shows the inbox/library
    # numbers move as reconcile files each orphan (no mid-pass rescan needed).
    pending_paths = {a.path for a in pending}
    base_library = sum(1 for a in albums if a.state in terminal)
    base_needs_sync = sum(1 for a in albums if a.state == AlbumState.NEEDS_SYNC)
    base_new = sum(1 for a in albums if a.state == AlbumState.NEW and a.path not in pending_paths)
    base_inbox = sum(1 for a in albums if a.state not in terminal and a.path not in pending_paths)

    completed = 0
    reconciled_bandcamp = 0
    reconciled_manual = 0
    recovered_url = 0  # store URL recovered but no MBID yet → NEEDS_MBID
    adopted = 0  # TAGGING album whose sidecar adopted an external file re-tag
    skipped = 0
    errors = 0

    def _report() -> None:
        if status_updater:
            stuck = skipped + errors
            status_updater(
                completed=completed,
                library=base_library + reconciled_manual,
                needs_sync=base_needs_sync + reconciled_bandcamp,
                new=base_new + stuck,
                # NEEDS_MBID (recovered URL) is inbox but not new/needs_sync/library.
                inbox=base_inbox + reconciled_bandcamp + recovered_url + stuck,
            )

    if status_updater:
        status_updater(total=total)
    _report()  # publish the base (all-zero deltas) before the first album

    # Split releases (#16), BEFORE the early return below: a release filed as
    # per-disc directories is made of COMPLETE albums, not of the orphans this
    # pass otherwise exists for, so gating it on `total` would mean it only ever
    # ran on a library that also happened to have something new in it.
    grouped = _promote_split_releases(albums, music_dir, exempt)

    # Expected track counts (#187), also before the early return: the albums
    # missing one are adopted COMPLETE albums, not orphans. AFTER the grouping
    # above, and that order is load-bearing — backfilling first would measure
    # each half of a split release against the whole release's track count and
    # flag both halves incomplete, which is true of neither.
    counted, newly_incomplete = (
        _backfill_track_counts(albums, exempt, fetch_track_count)
        if fetch_track_count is not None
        else (0, 0)
    )

    if not total:
        # Nothing to do. Reconcile runs on startup and after every sync, so
        # announcing a no-op made it the feed's most frequent content — three
        # lines to report that nothing happened (#101). Still logged, so it
        # stays greppable when someone asks "did reconcile run?".
        log.info("Reconcile: nothing to reconcile (no new albums on disk)")
        return {
            "total": 0,
            "reconciled_bandcamp": 0,
            "reconciled_manual": 0,
            "recovered_url": 0,
            "adopted": 0,
            "grouped": grouped,
            "counted": counted,
            "newly_incomplete": newly_incomplete,
            "skipped": 0,
            "errors": 0,
        }
    activity.record(f"Reconcile started — {total} album(s) to check")

    for album in pending:
        # One action scope per ALBUM (#84): reconcile writes a sidecar and an
        # activity entry for each, so each is separately revertible. A run-wide
        # scope would lump every album's audit records under a single id.
        with activity_store.action():
            label = f"{album.artist} / {album.title}"
            # The feed's album column: same "Artist — Title" shape the rest of the
            # log uses (the status bar keeps `label`'s " / " form).
            feed_label = f"{album.artist} — {album.title}".strip(" —")
            if status_updater:
                status_updater(current_item=label)
            try:
                sc = reconcile.reconcile_album(
                    album.path, fetch_urls=fetch_urls, recover_url=recover
                )
            except Exception as e:
                log.warning("Reconcile failed for %s: %s", label, e)
                errors += 1
                _report()
                continue
            # Record the resulting transition in the Activity feed (and server log).
            # Reconcile writes a sidecar; the scanner derives the state, but we know
            # the outcome here from the sidecar shape:
            #   MBID + store_url  → Needs Link   (tagged Bandcamp album)
            #   MBID, no store_url→ Library      (tagged, non-Bandcamp)
            #   store_url, no MBID→ Needs MBID   (recovered URL on an untagged album)
            #   None              → stays New    (nothing to reconcile)
            if sc is None:
                skipped += 1
                # Nothing to do (no MBID, no recoverable URL). Kept out of the feed —
                # it floods on a large untagged library; the status bar shows each
                # album as it's checked, and the closing summary reports the count.
                log.debug("%s: nothing to reconcile (no MBID or Bandcamp URL)", label)
            elif album.state == AlbumState.TAGGING:
                # The sidecar adopted the file tags (external Picard re-tag). The new
                # state (Library / Needs Link) settles on the post-reconcile rescan.
                adopted += 1
                # Id read back from disk AFTER reconcile_album wrote the sidecar —
                # it just moved the album's identity, so a pre-write id is dead (#65).
                activity.warning(
                    f"Adopted external re-tag — sidecar now {sc.mb_release_id}",
                    album_id=sidecar.album_id_for(album.path),
                    album_label=feed_label,
                )
            elif sc.mb_release_id and sc.store_url:
                reconciled_bandcamp += 1
                live_counts.move(AlbumState.NEW, AlbumState.NEEDS_SYNC)
                activity.record(
                    "New → Needs Link (reconciled from tags)",
                    album_id=sidecar.album_id_for(album.path),
                    album_label=feed_label,
                )
            elif sc.mb_release_id:
                reconciled_manual += 1
                # → Library; COMPLETE is the proxy bucket (the scan reset splits
                # COMPLETE/INCOMPLETE exactly — only the library *total* matters here).
                live_counts.move(AlbumState.NEW, AlbumState.COMPLETE)
                activity.record(
                    "New → Library (reconciled from tags)",
                    album_id=sidecar.album_id_for(album.path),
                    album_label=feed_label,
                )
            else:
                recovered_url += 1
                live_counts.move(AlbumState.NEW, AlbumState.NEEDS_MBID)
                activity.record(
                    "New → Needs MBID (recovered Bandcamp URL from tags)",
                    album_id=sidecar.album_id_for(album.path),
                    album_label=feed_label,
                )
            completed += 1
            _report()
        # No explicit pacing: reconcile_album now derives the store_url from the
        # embedded ©cmt URL (no network) for the common case, and the rare MB
        # url-rel lookups are already paced to 1/sec by musicbrainzngs's built-in
        # rate limiter (do_rate_limit=True). The old per-album sleep made a nuke
        # reconcile take ~16 min even though almost no album hit the network.

    adopted_note = f", {adopted} re-tag(s) adopted" if adopted else ""
    activity.record(
        f"Reconcile done: {reconciled_bandcamp + reconciled_manual + recovered_url} reconciled "
        f"({reconciled_bandcamp} → Needs Link, {reconciled_manual} → Library, "
        f"{recovered_url} → Needs MBID){adopted_note}, {skipped} unchanged, {errors} failed"
    )
    return {
        "total": total,
        "reconciled_bandcamp": reconciled_bandcamp,
        "reconciled_manual": reconciled_manual,
        "recovered_url": recovered_url,
        "adopted": adopted,
        "grouped": grouped,
        "counted": counted,
        "newly_incomplete": newly_incomplete,
        "skipped": skipped,
        "errors": errors,
    }


def _promote_split_releases(albums: list[Album], music_dir: Path, exempt: set[Path]) -> int:
    """Give each release found split across per-disc directories a sidecar on
    the parent, so the next scan reads it as one album. Returns how many.

    Idempotent by construction: `find_split_releases` skips any parent that
    already has a sidecar, so a second pass over the same library finds nothing
    and writes nothing. That matters because reconcile runs on startup and after
    every sync.

    `exempt` carries the same Forget intent the orphan pass respects — a parent
    the user just un-grouped must not be re-grouped underneath them.

    One activity entry per release, inside its own action scope so the audit row
    the promotion writes hangs off the entry describing it (#84). Failure of one
    release must not take down the reconcile pass around it — the albums stay
    exactly as they were, which is a state the app already handles, being the
    one it has always been in.
    """
    from harmonist import reconcile

    grouped = 0
    for split in reconcile.find_split_releases(albums, music_dir):
        if split.parent in exempt:
            # Forget on a grouped album deletes the parent's sidecar, which is
            # precisely how a user un-groups one. Re-grouping it on the very
            # next pass would make that button do nothing — the same reason
            # reconcile skips exempt orphans rather than re-deriving them.
            log.debug("split release at %s is forgotten — not re-grouping", split.parent)
            continue
        with activity_store.action():
            try:
                reconcile.promote_split_release(split)
            except Exception:
                log.exception("could not group the split release at %s", split.parent)
                activity.warning(
                    f"Could not group the discs under {split.parent.name} — left as separate albums"
                )
                continue
            grouped += 1
            activity.record(
                f"Grouped {len(split.parts)} disc folder(s) into one album "
                f"({split.track_count} tracks)",
                album_id=split.mb_release_id,
                album_label=split.parent.name,
            )
    return grouped


def _backfill_track_counts(
    albums: list[Album],
    exempt: set[Path],
    fetch_count: Callable[[str], int],
) -> tuple[int, int]:
    """Record the MB track count on every tagged album that has none, so the
    scanner can finally derive INCOMPLETE for it. Returns
    `(counted, newly_incomplete)`.

    One MusicBrainz request per album, which is a lot of requests — but bounded
    by the albums that lack the field, not by library size, and each one that
    succeeds removes itself from the candidate set permanently. So this is a
    long first pass over an adopted library and nothing at all thereafter. It
    reaches for the `media`-only lookup rather than the full release, which is
    ~25x smaller for the one number it needs.

    A failed lookup leaves the album alone and retries next pass. That is the
    right default for the overwhelmingly likely cause — the network, or MB
    being briefly unavailable — and the cost of being wrong is one repeated
    request per pass for a release MusicBrainz genuinely can't answer for.

    **No per-album activity entry for the ordinary case.** Reconcile runs on
    startup and after every sync, and several hundred "recorded a track count"
    lines is the flood the skip case is already careful to avoid; the sidecar
    write audits itself (`track_count_expected` is load-bearing), so the detail
    is on each album's own history page. An album that turns out to be
    INCOMPLETE does get its own entry — that is a defect the user has been
    unable to see until now, and it is the whole reason for the pass.
    """
    from harmonist import reconcile
    from harmonist.models import AlbumState

    pending = [a for a in albums if reconcile.needs_track_count(a) and a.path not in exempt]
    if not pending:
        log.debug("Track counts: nothing to backfill")
        return (0, 0)

    activity.record(f"Checking MusicBrainz for expected track counts — {len(pending)} album(s)")
    counted = 0
    newly_incomplete = 0
    for album in pending:
        with activity_store.action():
            try:
                count = reconcile.backfill_track_count(album.path, fetch_count=fetch_count)
            except Exception as e:
                # Warning, not exception: on a library-wide pass a stack trace
                # per unreachable album buries the run. The summary reports how
                # many were left, and the next pass retries them.
                log.warning("could not fetch the track count for %s: %s", album.path, e)
                continue
            if count is None:
                continue
            counted += 1
            if album.track_count < count:
                newly_incomplete += 1
                # COMPLETE until this instant only because nothing had ever
                # asked; keep the Library's counts honest between scans.
                live_counts.move(AlbumState.COMPLETE, AlbumState.INCOMPLETE)
                activity.record(
                    f"Missing {count - album.track_count} of {count} tracks",
                    album_id=sidecar.album_id_for(album.path),
                    album_label=f"{album.artist} — {album.title}".strip(" —"),
                )
    activity.record(
        f"Track counts recorded for {counted} album(s)"
        + (f" — {newly_incomplete} turned out to be incomplete" if newly_incomplete else "")
    )
    return (counted, newly_incomplete)
