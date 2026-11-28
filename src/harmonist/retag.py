"""Tag an album from a MusicBrainz release: the whole action, from the fetch to
the sidecar, for every caller — with a page in front of it or without one.

The tagger writes files; this is everything around that write that makes it a
tagging of an *album*: which release, which artwork candidate, every folder's
sidecar, and the bookkeeping a changed identity owes (a merge recorded, an
Ignore lifted, a pending download claimed).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from . import (
    activity,
    activity_store,
    album_files,
    artwork,
    audit,
    caa_listings,
    cover_art,
    formats,
    library_titles,
    match,
    mb_cache,
    mb_lookup,
    pending_downloads,
    reconcile,
    track_assignment,
)
from . import config as config_mod
from . import sidecar as sidecar_mod
from . import tagger as tagger_mod
from .models import IncludedPregap, Release, Sidecar
from .tagger import Tagger
from .transforms import NO_CHOICES, TaggingChoices, album_title
from .url_recovery import album_slug

log = logging.getLogger(__name__)

# For a `log.exception` whose news the Activity feed already carries as its own
# entry: the traceback belongs in the server log, and without the flag the
# feed's WARNING+ mirror (activity._ActivityLogHandler) adds a second row (#461).
_LOG_ONLY = {"_activity": True}


def _claim_pending_by_store_url(store_url: str | None) -> None:
    """Drop any potential download whose Bandcamp slug matches `store_url` — the
    purchase is now represented on disk (just tagged), so it mustn't linger as a
    pending download. Subdomain-insensitive via `album_slug`. Covers confirming a
    mis-tag (store_url set to the owned edition's URL) and any tag that resolves a
    pending purchase."""
    if not store_url:
        return
    slug = album_slug(store_url)
    if not slug:
        return
    for p in pending_downloads.all_pending():
        if album_slug(p.url) == slug:
            pending_downloads.remove(p.item_id)


def _record_merge(album_path: Path, old_mbid: str, new_mbid: str, label: str) -> None:
    """Say that MusicBrainz merged one release into another (#268).

    Called AFTER the sidecar write, for two reasons. The album's id has just
    moved with it, and an entry stamped with the pre-write id would hang off a
    release the sidecar no longer names (#65). And the entry is a report of
    something that happened, not of something attempted — a tagging that threw
    part-way should not leave a line claiming the identity moved.

    The alias that keeps the album's pre-merge history reachable is NOT recorded
    here: `sidecar.write()` already records one whenever the canonical id
    changes, and this is one of those. Adding a call would duplicate it at one
    call site while leaving every other identity change relying on the write —
    the shape the event-recording skill warns about.

    Both sinks, because they answer different questions. The activity entry is
    the outcome in the user's language, and the audit line is the pair of ids —
    which is forensics, and doesn't belong in prose the feed renders next to an
    album name it already shows in its own column.

    `label` is passed IN rather than built here, and it is the same string the
    tagging's own entry carries — `tagger.album_label`, off the release this
    press wrote (#571). It used to be `album_path.name`, which is the album's
    FOLDER: one press then wrote two adjacent feed rows about one album, naming
    it "Artist — Title" on the tagging and the bare directory on the merge, with
    both names rendered as links. Deriving it here from the path again is how
    that comes back, so the caller hands over the name it has already settled on.
    """
    album_id = sidecar_mod.album_id_for(album_path)
    audit.record(
        "release.merged",
        album_id=album_id,
        album=album_path,
        old=old_mbid,
        new=new_mbid,
    )
    activity.record(
        "MusicBrainz merged the release this album named into another one — "
        "it now follows the surviving release",
        album_id=album_id,
        album_label=label,
    )


class ConfirmationChanged(Exception):
    """The selected release no longer describes the confirmation preview."""


def release_fingerprint(
    release: Release, requested_mbid: str, tagging: TaggingChoices = NO_CHOICES
) -> str:
    # Bind the selected candidate to the canonical snapshot. A merge stores
    # only the surviving ID; continuing review must find that same row locally.
    digest = hashlib.sha256(
        json.dumps([requested_mbid, release], sort_keys=True).encode()
    ).hexdigest()
    title = album_title(release, tagging.transforms, tagging.library_releases)
    title_digest = hashlib.sha256(title.encode()).hexdigest()
    return f"{release['id']}:{digest}:{title_digest}"


def reviewed_release(mbid: str, fingerprint: str, tagging: TaggingChoices = NO_CHOICES) -> Release:
    """Resolve a reviewed snapshot locally; never substitute a different one."""
    canonical, separator, _ = fingerprint.partition(":")
    release = mb_cache.stored_release(canonical) if separator else None
    if release is None:
        raise ConfirmationChanged(
            "The reviewed release is no longer available. Refresh MusicBrainz and review again."
        )
    current = release_fingerprint(release, mbid, tagging)
    if current != fingerprint:
        what = (
            "MusicBrainz"
            if current.split(":")[1] != fingerprint.split(":")[1]
            else "The title choice"
        )
        raise ConfirmationChanged(
            f"{what} changed since the preview. Refresh the comparison and review again."
        )
    return release


def _accepted_after_tagging(
    accepted: str | None, requested: str, tagged: str, *, accept: bool
) -> str | None:
    """The release the user bought, after tagging `requested` came back as `tagged` (#618).

    A re-tag of the accepted release keeps it, and a merge (`tagged` differing
    from `requested`) renames that release rather than replacing it, so
    acceptance follows the id. Any other rematch clears it: kept, it would
    silently return if the album were ever matched back.
    """
    return tagged if accept or accepted == requested else None


def check_review(
    album_path: Path,
    mbid: str,
    cfg: config_mod.Config,
    *,
    paths: Sequence[Path],
    expected_release: str,
    assignment_draft: track_assignment.Draft | None,
) -> None:
    """Refuse a reviewed confirmation whose review no longer holds, with the
    error `tag_with_release` would raise, while the review is still open to
    show it (#1053).

    The write itself happens later, on another thread, once the request has
    answered and the review has closed. It makes these same checks again, so
    anything that changes in between is still refused, but by then all it can
    do is report the refusal. The answer that belongs in the review (the release
    or the files changed since it was shown) is given here. Spends no
    MusicBrainz request: a review is checked against its stored snapshot.
    """
    _, _, assignment, _ = _inputs(
        album_path,
        mbid,
        cfg,
        paths=paths,
        expected_release=expected_release,
        assignment_draft=assignment_draft,
    )
    if assignment is not None:
        tagger_mod.refuse_unassigned_track_ids(
            [path for path in album_files.for_paths(paths) if path not in assignment]
        )


def _inputs(
    album_path: Path,
    mbid: str,
    cfg: config_mod.Config,
    *,
    paths: Sequence[Path] | None,
    expected_release: str | None,
    assignment_draft: track_assignment.Draft | None,
) -> tuple[Release, TaggingChoices, dict[Path, int] | None, tuple[IncludedPregap, ...] | None]:
    """The release, spelling choices and track assignment a tagging writes,
    refusing one whose review no longer holds before anything is written."""
    # Reviewed actions apply what the user saw. Unreviewed tagging still asks
    # fresh; the exception must not silently spread to automatic operations.
    if expected_release is not None:
        tagging = library_titles.choices(cfg.tagging.choices(), excluding=paths or [album_path])
        release = reviewed_release(mbid, expected_release, tagging)
    else:
        release = mb_cache.fetch_release(mbid, max_age=mb_cache.FRESH)
        tagging = library_titles.choices(cfg.tagging.choices(), excluding=paths or [album_path])
    if assignment_draft is not None:
        current_sidecar = sidecar_mod.read(album_path)
        panel = track_assignment.panel(
            album_files.for_paths(paths or [album_path]),
            release,
            assignment_draft,
            confirmed=bool(current_sidecar and current_sidecar.mb_release_id == release["id"]),
        )
        return release, tagging, panel.mapping(), panel.pregap_decisions()
    # Refuse before artwork lookup or any write, including callers that
    # supply their own Tagger. The tagger itself enforces the same guard.
    files = album_files.for_paths(paths or [album_path])
    review = tagger_mod.assignment_review(release, [formats.read_tags(f) for f in files])
    if review.required:
        raise tagger_mod.TrackAssignmentRequired(
            files=len(files),
            tracks=len(match.mb_track_lengths(release)),
            unassigned=review.unassigned,
        )
    return release, tagging, None, None


def tag_with_release(
    album_path: Path,
    mbid: str,
    cfg: config_mod.Config,
    tagger: Tagger,
    *,
    incomplete: bool = False,
    store_url_override: str | None = None,
    overwrite_art: bool = False,
    paths: Sequence[Path] | None = None,
    expected_artwork: str | None = None,
    artwork_included: bool = True,
    scope: artwork.Scope | None = None,
    chosen: artwork.Source | None = None,
    choices: artwork.Choices | None = None,
    expected_release: str | None = None,
    assignment_draft: track_assignment.Draft | None = None,
    release_artwork_only: bool = False,
    accept_release: bool = False,
) -> tagger_mod.TaggingOutcome:
    """Fetch MB release, fetch cover, write tags, update sidecar.

    `accept_release` records the release as the one the user bought (#618):
    choosing it from the mismatch review's candidates and confirming its track
    review already said so, and asking again would be a second confirmation.

    Returns what the tagging actually DID, in its two halves (#482). It used to
    return None, which left `TaggingOutcome.artwork_withheld` with no reader
    anywhere: a re-tag that tagged the album and refused its artwork said only
    "Re-tagged", and the reason reached the user only if they went looking in
    the feed. Callers with nothing to report are free to ignore it.

    `expected_artwork` is the fingerprint of the artwork the album page showed,
    when a page is who asked (#469). A tagging whose plan no longer matches it
    writes the tags and no artwork, and says so in the outcome.

    `artwork_included=False` leaves the artwork half out altogether — what the
    album page's artwork checkbox posts when unticked (#482). Distinct from a
    fingerprint that failed to match: that is a stale preview the user is owed a
    warning about, this is a choice they just made.

    `expected_release` binds confirmation to the stored release shown in its
    review (#532). Apply that snapshot without a request, whatever its age.
    A missing or changed snapshot requires review before any writes. Artwork
    is already cached by the review; do not fetch a different image.

    `scope` is how much of the plan to write, and must agree with the scope
    `expected_artwork` was taken at or the comparison mismatches on every press.
    None keeps the tagger's own derivation, which is what every caller without a
    page in front of it wants.

    `mbid` is what to ask MusicBrainz for, not necessarily what the album ends
    up tagged as: a merged release redirects, and this follows the release it
    actually got (#268). See the rebinding below.

    `incomplete=True` runs the tagger in incomplete mode (file_count <
    MB track count allowed). Nothing is persisted about the count: the tagging
    writes the release's own totals into every file, and that is what the
    scanner derives INCOMPLETE from afterwards (#195).

    `store_url_override` replaces the sidecar's store_url. Used when confirming
    a mis-tag: the album is actually the *owned* edition, so its store_url must
    become the URL where the user purchased it (the candidate's
    `mistag_owned_url`) — otherwise the old (wrong-edition) URL matches no
    purchase and the album can never link, falling through to surrender.
    """
    release, tagging, assignment, pregap_decisions = _inputs(
        album_path,
        mbid,
        cfg,
        paths=paths,
        expected_release=expected_release,
        assignment_draft=assignment_draft,
    )
    # MusicBrainz REDIRECTS a merged MBID, so the release that comes back can
    # carry a different id from the one asked for. That difference IS the merge
    # notification — cheap, exact, and the only one there is (#268). Everything
    # downstream follows the id the release actually has, because the tagger
    # writes `release["id"]` into the files either way: disagreeing with it left
    # the sidecar naming a release that no longer exists, the album deriving
    # TAGGING off its own files, and reconcile rewriting the identity through a
    # path meant for "the user re-tagged in Picard".
    #
    # `mbid` is REBOUND rather than a second name being introduced, so the
    # correction holds by construction: a line added below that reaches for the
    # album's release gets the one it was tagged as, not the one that was asked
    # for. Only the merge check itself wants the original.
    #
    # Unconditional, and it stays unconditional under #32's unattended pass:
    # there is nothing to authorise, because the merge has already happened on
    # MusicBrainz and this is only noticing it. Undo is the escape hatch, as it
    # is for any tagging. Not the significance classifier's business (#267) —
    # that decides which TAG changes may auto-apply, and this is an identity
    # fact arriving beside them.
    requested_mbid, mbid = mbid, release["id"]
    rg = release.get("release-group") or {}
    cover_path = cover_art.cached_cover(album_path) if artwork_included else None
    archive: cover_art.Front | None = None
    if artwork_included and release_artwork_only:
        # Pin the validated bytes before tagging. Passing None would let the
        # tagger independently reuse a group image from the shared cache.
        archive = cover_art.cached_front(mbid, release_only=True)
        if archive is None:
            raise ConfirmationChanged(
                "Artwork changed since the review. Go back and review the artwork again."
            )
    try:
        # Asked only when the album has no folder cover — the one case a
        # tagging has always spent a request on, and the one where the archive
        # may be what the cover is created from. The answer is a CANDIDATE: the
        # tagger's plan decides whether it wins (#469). An album whose folder
        # cover already exists weighs the archive only from the cache, which
        # the album page's own check fills.
        if (
            artwork_included
            and not release_artwork_only
            and cover_path is None
            and expected_release is None
        ):
            archive = cover_art.front_image(release["id"], rg.get("id"))
    except cover_art.CoverArtError:
        # Tagging is the work; the cover is a side effect of it (#458). Design
        # §"Cover art (mandatory)" already rules that an album with no cover
        # available is tagged anyway — so an archive that could not ANSWER must
        # not produce a worse outcome than one that answered "there is none".
        # Losing a whole re-tag to a transient 503 is exactly that inversion.
        #
        # `archive=None` is a value the tagger already handles: the plan weighs
        # what the album has, and a folder cover may still be created from the
        # album's own artwork.
        #
        # Loud in both channels, because the two readers are different: the log
        # is all there is at 3am, and the activity line is how someone who
        # pressed a button finds out their album is now tagged without the cover
        # it should have. Neither is a failure of the tagging, so neither claims
        # to be one.
        #
        # `_LOG_ONLY` because `_ActivityLogHandler` mirrors every WARNING+ record
        # on the `harmonist` logger into the feed, so without it this line lands
        # there too and the album gets the same news twice (#461). The mirror is
        # the copy worth losing: it carries this raw absolute path — a container
        # path under Docker, which is why audit records are relativised — and no
        # album id, so it floats attributed to nothing.
        log.exception(
            "Cover Art Archive unavailable for %s — tagging without it",
            album_path,
            extra=_LOG_ONLY,
        )
        activity.warning(
            "Cover Art Archive unavailable — tagged without its artwork",
            album_id=sidecar_mod.album_id_for(album_path),
            album_label=tagger_mod.album_label(release, album_path),
        )
    outcome = tagger.tag_and_artwork(
        album_path,
        release,
        cover_path=cover_path,
        incomplete=incomplete,
        overwrite_art=overwrite_art,
        # An album can span several directories (#197); `album_path` is only its
        # primary one, so tagging what is under that alone would leave the rest
        # of the album on its old tags.
        files=album_files.for_paths(paths) if paths else None,
        archive=archive,
        expected_artwork=expected_artwork,
        artwork_included=artwork_included,
        scope=scope,
        # The candidate the user chose, so the plan rebuilt at write time is the
        # one they were shown rather than the one the size rule prefers (#488) —
        # and the ones chosen row by row, for the same reason (#659).
        chosen=chosen,
        choices=choices,
        picks=caa_listings.picked_originals(mbid, choices or {}),
        assignment=assignment,
        # The album page built its preview under this same policy, so the plan
        # rebuilt at write time is the one the fingerprint was taken of (#516).
        folder_cover=cfg.tagging.folder_cover,
        # …and under these spelling settings, so what this writes is what the
        # page's MusicBrainz column showed (#544).
        tagging=tagging,
    )

    sc = sidecar_mod.read(album_path)
    store_url = store_url_override or (sc.store_url if sc else None)
    if store_url is None:
        # No store_url yet (e.g. a manual download assigned an MBID directly).
        # Derive the Bandcamp store URL so a purchase lands in Needs Linking rather
        # than Complete: embedded ©cmt URL → MB url-rel → artist-root placeholder,
        # all gated by ©cmt Bandcamp evidence. Best-effort — never blocks tagging.
        try:
            store_url = reconcile.store_url_for_tagging(
                album_path,
                mbid,
                fetch_urls=mb_cache.stored_release_urls
                if expected_release is not None
                else mb_cache.fetch_release_urls,
            )
        except Exception:
            log.exception("store_url derivation during tagging failed")
    # `replace`, not a fresh `Sidecar(...)` — anything not named here carries
    # through BY CONSTRUCTION (#239). Listing the fields to keep meant every
    # field left off the list was silently reset to its default on every
    # re-tag: `video_media` (so an album with a bonus DVD went Incomplete until
    # a later pass spent a MusicBrainz request re-learning it), and the
    # `purchase_unavailable` / `tracks_unavailable` surrenders, whose whole
    # purpose is to be permanent. A field added to the model later would have
    # joined them, silently, with no test to notice.
    tagged_at = datetime.now(UTC)
    tagged_files = album_files.for_paths(paths or [album_path])
    video_media = mb_lookup.video_media_of(release)
    for folder in paths or [album_path]:
        # All tagged folders must follow the release, not just the primary
        # one (#730). Preserve each sidecar's own provenance and decisions;
        # copying the primary sidecar would silently replace the other parts'.
        base = (sc if folder == album_path else sidecar_mod.read(folder)) or Sidecar()
        new = replace(
            base,
            store_url=store_url if folder == album_path else store_url_override or base.store_url,
            added_at=base.added_at or tagged_at,
            mb_release_id=mbid,
            mb_match_candidate=None,  # cleared on tag — a suggestion, now acted on
            tagged_at=tagged_at if outcome.tags_changed else base.tagged_at or tagged_at,
            included_pregaps=pregap_decisions
            if pregap_decisions is not None
            else base.included_pregaps,
            # Read off the release this tagging used, so the one fact the files
            # cannot carry (#206) is recorded at the moment it is known rather than
            # left for a reconcile pass to fetch again. Pure — no request (#237).
            video_media=video_media,
            accepted_release_id=_accepted_after_tagging(
                base.accepted_release_id, requested_mbid, mbid, accept=accept_release
            ),
            mismatch_dismissal=None if accept_release else base.mismatch_dismissal,
            # Folded into the same write rather than a second one after it (#663).
            borrowed_artwork=caa_listings.stand_ins_after(
                album_path,
                tagged_files,
                mbid,
                base.borrowed_artwork,
                outcome.artwork.written,
            ),
            artwork_watch=(
                None
                if outcome.artwork.written
                and not (outcome.artwork.stale or outcome.artwork.unkept or outcome.artwork.failed)
                else base.artwork_watch
            ),
        )
        sidecar_mod.write(folder, new)
    # The files now carry what MusicBrainz says, so there is nothing left to be
    # waiting for and an Ignore has nothing to mute (#271). Left behind, it would
    # mute the NEXT divergence on this album if MusicBrainz happened not to have
    # moved in between — an external re-tag in Picard is the way that happens.
    #
    # Under BOTH ids, because a merge moves the album's identity right here: the
    # bookmark was written under the id the user was looking at, which is the one
    # that was requested rather than the one that came back.
    activity_store.unignore_update(mbid)
    if mbid != requested_mbid:
        activity_store.unignore_update(requested_mbid)
        _record_merge(album_path, requested_mbid, mbid, tagger_mod.album_label(release, album_path))
    _claim_pending_by_store_url(store_url)
    return outcome
