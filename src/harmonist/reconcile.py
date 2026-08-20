"""Per-album reconciliation: derive a sidecar from existing tags + MB lookup.

For an album that's already tagged (has a `MusicBrainz Album Id` atom) but
has no `.harmonist.json` sidecar, reconcile_album decides what `store_url`
to record (if any):

  * If the file's `©cmt` tag contains any `bandcamp.com` URL **and** MB has
    a Bandcamp URL relationship for the release → `store_url` set to MB's
    canonical URL, `bandcamp.item_id=None`. The album shows as NEEDS_SYNC
    until the next sync fills in item_id by matching against the user's
    purchase list.

  * Otherwise → no `store_url`. Album shows as DONE.

If the album has **no** MBID atom (e.g. a Bandcamp download added by hand,
never run through Picard), we instead try to recover its Bandcamp store URL
from the `©cmt` comment. On success we write a sidecar with that `store_url`
and no MBID, so the album advances NEW → NEEDS_MBID (then, once tagged,
NEEDS_SYNC picks up its Bandcamp item_id). Without this an untagged download
would sit in NEW forever, or tag straight to COMPLETE and never sync.

Pure: no globals. Caller injects `fetch_urls` (MB lookup) and `recover_url`
(Bandcamp URL recovery) so tests don't need real network.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from . import album_files, audit, formats, url_recovery
from . import sidecar as sidecar_mod
from .formats import owned
from .models import Album, Sidecar, is_bandcamp_url

log = logging.getLogger(__name__)


def reconcile_album(
    album_dir: Path,
    *,
    fetch_urls: Callable[[str], list[str]],
    recover_url: Callable[[Path], str | None] = url_recovery.recover_store_url,
) -> Sidecar | None:
    """Inspect the album, write a sidecar, return it. None if nothing to do.

    Two jobs:
      * No sidecar → derive one from the file tags (or recover a Bandcamp URL).
      * Sidecar present but its `mb_release_id` disagrees with a *consistent*
        file MBID → **adopt the file tags** (the user re-tagged in Picard, as we
        ask them to). Otherwise leave an existing sidecar untouched.
    """
    files = album_files.audio_files(album_dir)
    if not files:
        return None

    existing: Sidecar | None = None
    if sidecar_mod.has_sidecar(album_dir):
        try:
            existing = sidecar_mod.read(album_dir)
        except Exception:
            return None  # unreadable sidecar — don't touch it
    if existing is not None:
        # Adopt an external re-tag: the files now carry a different consistent
        # MBID than the sidecar records (the TAGGING-state mismatch). Files win —
        # re-point the sidecar, keeping store_url / item_id (same purchase), and
        # clear the now-stale candidate + expected-track-count. Scoped to a
        # sidecar that HAS an MBID, so a surrendered album (no MBID, but files
        # still carry the old one) is NOT re-promoted.
        file_mbid = _consistent_file_mbid(files)
        if existing.mb_release_id and file_mbid and file_mbid != existing.mb_release_id:
            adopted = replace(
                existing,
                mb_release_id=file_mbid,
                mb_match_candidate=None,
                tagged_at=datetime.now(UTC),
            )
            sidecar_mod.write(album_dir, adopted)
            return adopted
        return None  # sidecar present + consistent (or files untagged) → leave it

    mbid, _comment = _read_album_id_and_comment(files)
    now = datetime.now(UTC)

    if not mbid:
        # No MBID atom — try to recover the Bandcamp store URL from the comment
        # so the album can advance NEW → NEEDS_MBID instead of stalling in NEW.
        return _reconcile_untagged(album_dir, recover_url, now)

    # Derive the store_url EMBEDDED-FIRST (the ©cmt's precise /album/ URL — the
    # actual purchase URL), falling back to an MB url-rel only when there's no
    # precise embedded URL. The MB-first path made one rate-limited MB call PER
    # album (~16 min to reconcile a 960-album library after a nuke); the embedded
    # URL needs no network and is a better match key (it's what the user bought).
    bandcamp_url = store_url_for_tagging(album_dir, mbid, fetch_urls=fetch_urls)
    sc = Sidecar(
        store_url=bandcamp_url,
        mb_release_id=mbid,
        added_at=now,
        tagged_at=now,
    )
    sidecar_mod.write(album_dir, sc)
    return sc


def _reconcile_untagged(
    album_dir: Path, recover_url: Callable[[Path], str | None], now: datetime
) -> Sidecar | None:
    """For an album with no MBID atom: recover its Bandcamp store URL (if any)
    and record it. Returns the sidecar (NEEDS_MBID — no MBID, no tagged_at), or
    None when no URL is recoverable (album stays an Orphan)."""
    try:
        recovered = recover_url(album_dir)
    except Exception as e:
        log.warning("URL recovery failed for %s: %s", album_dir, e)
        return None
    if not recovered:
        return None
    sc = Sidecar(
        store_url=recovered,
        mb_release_id=None,  # untagged — lands in NEEDS_MBID, not NEEDS_SYNC
        added_at=now,
    )
    sidecar_mod.write(album_dir, sc)
    return sc


def reconcile_pending(
    album_dirs: list[Path],
    *,
    fetch_urls: Callable[[str], list[str]],
    recover_url: Callable[[Path], str | None] = url_recovery.recover_store_url,
) -> dict[str, int]:
    """Reconcile a batch of album dirs. Returns a stats summary."""
    stats = {"reconciled_bandcamp": 0, "reconciled_manual": 0, "skipped": 0, "errors": 0}
    for d in album_dirs:
        try:
            sc = reconcile_album(d, fetch_urls=fetch_urls, recover_url=recover_url)
        except Exception as e:
            log.warning("reconcile failed for %s: %s", d, e)
            stats["errors"] += 1
            continue
        if sc is None:
            stats["skipped"] += 1
        elif sc.store_url:
            stats["reconciled_bandcamp"] += 1
        else:
            stats["reconciled_manual"] += 1
    return stats


def _consistent_file_mbid(files: list[Path]) -> str | None:
    """The single MB Album Id shared by all tagged files, or None if no file is
    tagged or they disagree (we don't pick a winner among inconsistent tags —
    those are surfaced as INCONSISTENT for the user to split into folders)."""
    ids = {mid for f in files if (mid := formats.read_album_id(f))}
    return next(iter(ids)) if len(ids) == 1 else None


def _read_album_id_and_comment(files: list[Path]) -> tuple[str | None, str]:
    """Return (mbid, comment) from the first file that has an MBID atom."""
    for f in files:
        mbid = formats.read_album_id(f)
        if mbid:
            return mbid, formats.read_comment(f) or ""
    return None, ""


def store_url_for_tagging(
    album_dir: Path,
    mbid: str,
    *,
    fetch_urls: Callable[[str], list[str]],
) -> str | None:
    """The best deterministic Bandcamp store URL for an album being tagged to
    `mbid`, or None — used at tag time so a manually-assigned download reaches
    Needs Link (not Complete) when it's a Bandcamp purchase.

    No guessing, three sources in preference order (precise first):
      1. The fully-formed `/album/` (or `/track/`) URL embedded in the file's
         `©cmt` — the actual purchase URL.
      2. MB's canonical Bandcamp URL for the release (a precise `/album/` URL).
      3. Last resort: the artist-root Bandcamp URL from the `©cmt` (e.g.
         `artist.bandcamp.com`) as a placeholder — enough to mark the album a
         Bandcamp purchase; the sync then links it to a purchase by title.

    Everything is gated by Bandcamp evidence in the `©cmt`: with no Bandcamp URL
    in the comment at all, returns None (a CD rip stays Complete, not Needs Link).
    """
    files = album_files.audio_files(album_dir)
    if not files:
        return None
    comment = formats.read_comment(files[0]) or ""
    url = url_recovery.extract_bandcamp_url(comment)
    if url is None:
        # No Bandcamp evidence → not treated as a Bandcamp purchase (→ Complete,
        # not Needs Link). Observability: when there IS a comment but no
        # bandcamp.com URL (e.g. "Visit https://3six.net", or a Picard-stripped
        # tag), log it so a genuinely-purchased album silently landing in Library
        # is explainable from the logs — the fuzzy potential-download match is the
        # recovery path for these. Empty comments (plain CD rips) aren't logged.
        if comment.strip():
            log.info(
                'no Bandcamp store URL for "%s": ©cmt has no bandcamp.com URL: %r',
                album_dir.name,
                comment[:160],
            )
        return None
    # 1. A precise release URL embedded in the comment is the real purchase URL.
    if "/album/" in url or "/track/" in url:
        return url
    # 2. Otherwise prefer MB's canonical /album/ URL when it has one.
    if mb_url := matching_bandcamp_url(mbid, comment, fetch_urls):
        return mb_url
    # 3. Fall back to the artist-root URL as a placeholder (→ title-linked on sync).
    return url


def matching_bandcamp_url(
    mbid: str,
    comment: str,
    fetch_urls: Callable[[str], list[str]],
) -> str | None:
    """Return MB's canonical Bandcamp URL for the release, or None.

    Requires BOTH:
      - The file's ©cmt tag mentions a bandcamp.com URL (evidence of purchase).
      - MB has at least one Bandcamp URL relationship for the release.

    Used as the fallback when no fully-formed Bandcamp URL is embedded in the
    comment; the recorded URL is MB's canonical one.
    """
    if url_recovery.extract_bandcamp_url(comment) is None:
        return None
    try:
        urls = fetch_urls(mbid)
    except Exception as e:
        log.warning("MB url-rels lookup failed for %s: %s", mbid, e)
        return None
    for url in urls:
        if is_bandcamp_url(url):
            return url
    return None


# ---------------------------------------------------------------------------
# Split releases: one MB release living in several per-disc directories (#16)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SplitRelease:
    """A release whose discs the user filed in sibling directories.

    `parent` is the directory that should own the album; `parts` are the
    per-disc directories beneath it, in disc order. Detection only — promoting
    it is a separate, audited step.

    `artist` and `title` come from the parts' TAGS, not from any directory name.
    The grouping parent is often a container the user named for something else —
    the LOTR trilogy's discs sit directly under `Howard Shore/`, so the entry
    announcing it read "Howard Shore · Grouped 3 disc folder(s)" for an album
    actually called *The Lord of the Rings Trilogy: The Motion Picture Trilogy
    Soundtrack* (#191). Every other entry in the feed names an album from its
    tags; this one had only a path to hand.
    """

    parent: Path
    mb_release_id: str
    parts: tuple[Path, ...]
    artist: str = ""
    title: str = ""

    @property
    def label(self) -> str:
        """ "Artist — Title" for the feed, falling back to the parent's directory
        name when the tags carry neither (an album with no album title at all —
        the scanner already falls back to the directory name for that case)."""
        joined = f"{self.artist} — {self.title}".strip(" —")
        return joined or self.parent.name

    @property
    def track_count(self) -> int:
        return sum(len(album_files.audio_files(p)) for p in self.parts)


def find_split_releases(albums: list[Album], music_dir: Path) -> list[SplitRelease]:
    """Directories whose subdirectories hold the discs of one MB release.

    The test is an identity match, not a best fit — every one of these has to
    hold, and any that doesn't leaves the directories exactly as they are:

    * the parent is not the library root, and is not an album itself (it has no
      audio of its own, or the scanner would have yielded it);
    * it has no sidecar yet — one already there means it is grouped already,
      and re-promoting it every pass would be the oscillating rewrite the
      idempotence rule forbids;
    * at least two audio-bearing subdirectories, and EVERY one of them is
      accounted for — a leftover means this is a container directory that
      happens to hold two discs, not a release that occupies the whole of it;
    * every part is tagged to the same `mb_release_id` — the exact-scoped-unique
      match, scoped to this one parent, from sidecars that already exist;
    * the parts hold DIFFERENT TRACKS of that release, which is what separates a
      split release from two duplicate copies of one disc. See
      `_parts_hold_different_tracks`.

    Deliberately makes NO MusicBrainz call: it runs over the whole library, and
    the budget rule (§6) puts a per-album lookup out of reach. The only check
    that reads tags or touches the filesystem is the last one, by which point a
    directory has already had to look exactly like a split release.
    """
    by_parent: dict[Path, list[Album]] = {}
    album_paths = {a.path for a in albums}
    for album in albums:
        parent = album.path.parent
        if parent == music_dir or parent in album_paths:
            continue
        by_parent.setdefault(parent, []).append(album)

    found = []
    for parent, parts in sorted(by_parent.items()):
        if len(parts) < 2 or sidecar_mod.has_sidecar(parent):
            continue
        if any(a.sidecar is None for a in parts):
            continue
        mbids = {a.sidecar.mb_release_id for a in parts if a.sidecar is not None}
        if len(mbids) != 1:
            continue
        mbid = next(iter(mbids))
        if mbid is None:
            continue
        # Left until last deliberately: these are the only checks that read tags
        # or touch the filesystem, and this loop runs over every directory
        # holding two or more albums — i.e. every artist directory in the
        # library, on every reconcile pass. The release check above rejects
        # those for free (two albums by one artist are two releases), so the
        # reads below only ever happen for a directory that already looks like a
        # split release, and only once: the parent gets a sidecar and is skipped
        # from then on.
        if _has_unaccounted_audio(parent, {a.path for a in parts}):
            continue
        if not _parts_hold_different_tracks(parts):
            continue
        ordered = _in_disc_order(parts)
        found.append(
            SplitRelease(
                parent=parent,
                mb_release_id=mbid,
                parts=tuple(a.path for a in ordered),
                # From the scan's own reading of the tags — `build_album` has
                # already resolved album-artist vs per-track artist for these.
                artist=ordered[0].artist,
                title=ordered[0].title,
            )
        )
    return found


def _parts_hold_different_tracks(parts: list[Album]) -> bool:
    """True when the candidate parts hold DIFFERENT tracks of the release.

    The question this whole feature turns on. "Same release, two folders"
    describes a split release and a duplicate pair equally well, and merging a
    duplicate would fabricate a two-disc album out of two copies of one disc —
    so something has to tell them apart.

    **Release-track MBIDs, when the files carry them.** Every track of a release
    has its own `MusicBrainz Release Track Id`, so two directories holding
    different discs have DISJOINT sets and two copies of one disc have identical
    ones. This is a direct reading of the thing in question rather than a proxy
    for it: it does not merely establish that the parts *claim* to be different
    discs, it establishes that they *are* different tracks. Picard has written
    this tag for well over a decade, and Harmonist's own tagger writes it, so it
    is present on anything tagged the way the adoption path asks for.

    **Distinct disc numbers, when they don't.** A pre-2011 rip may carry no
    track ids at all, and refusing to ever group those would be a needless
    limitation. Distinct disc numbers are weaker — they are what the files
    claim, not what they contain — but they are still an exact test, and a
    duplicate pair fails it (both copies say disc 1).

    A MIXTURE is rejected: if one part has track ids and another doesn't, there
    is nothing to compare, and the disc-number fallback would be answering a
    question the better evidence was available to settle.
    """
    identities = [_release_track_ids(a.path) for a in parts]
    if all(ids for ids in identities):
        # Disjoint iff no id is shared, i.e. the union is as big as the parts.
        return len(set().union(*identities)) == sum(len(ids) for ids in identities)
    if any(ids for ids in identities):
        return False  # some tagged, some not — no honest comparison
    discs = [a.disc_num for a in parts]
    return all(d is not None for d in discs) and len(set(discs)) == len(discs)


def _release_track_ids(album_dir: Path) -> frozenset[str]:
    """Every file's `MusicBrainz Release Track Id` in this directory, or an
    empty set if ANY file lacks one.

    All-or-nothing because a partial set makes disjointness meaningless: two
    copies of one disc where half the files are untagged would compare as
    disjoint on the tagged half and read as a split release.
    """
    ids = set()
    for f in album_files.audio_files(album_dir):
        try:
            value = formats.read_owned(f).get(owned.Owned.MB_RELEASE_TRACK_ID)
        except Exception:
            # An unreadable file is not evidence of anything. Bail rather than
            # judging the album on the files that happened to open — grouping is
            # a write, and this is the check standing between it and a duplicate.
            log.warning("could not read the track id on %s — not grouping", f)
            return frozenset()
        if not isinstance(value, str) or not value:
            return frozenset()
        ids.add(value)
    return frozenset(ids)


def _in_disc_order(parts: list[Album]) -> list[Album]:
    """Candidate parts in disc order, falling back to path order when the files
    carry no disc number. Affects only how the promotion is recorded — the
    album's own track order comes from `album_files`."""
    if all(a.disc_num is not None for a in parts):
        return sorted(parts, key=lambda a: a.disc_num or 0)
    return sorted(parts, key=lambda a: a.path)


def _has_unaccounted_audio(parent: Path, parts: set[Path]) -> bool:
    """True when audio lives under `parent` outside the candidate parts —
    directly in it, or in a subdirectory the scan did not yield as an album
    (an unreadable sidecar, say). Such a directory is a container that happens
    to hold two discs, and absorbing the rest of it would be a guess.
    """
    try:
        entries = list(parent.iterdir())
    except OSError:
        return True  # can't rule it out → don't merge
    for entry in entries:
        if entry.is_file() and formats.is_supported(entry):
            return True
        if entry.is_dir() and entry not in parts and album_files.descendant_audio_files(entry):
            return True
    return False


def promote_split_release(split: SplitRelease) -> Sidecar:
    """Write the parent's sidecar so the discs read as one album, and return it.

    **Nothing on disk moves.** The files stay exactly where the user filed them,
    keeping the layout Plex and Navidrome already index; all that changes is
    which directory carries the `.harmonist.json`, and from the next scan the
    parent answers for the whole release (see `album_files`). Removing that
    sidecar is what undoes it — the parts then stand on their own again,
    unchanged — so this needs no migration and offers a way back out.

    The parts' own sidecars are left alone. They are stale descriptions of
    directories that are no longer albums, and deleting them would be a
    destructive write to user data on the strength of a derived rule; the
    scanner ignores them (it never descends into a grouped album), so they cost
    nothing but a little clutter.

    The parent's sidecar inherits from the parts rather than being re-derived,
    which is what keeps this off the MusicBrainz budget: they already agree on
    the release, and the store link, purchase state and timestamps are the same
    album's history however many directories it was living in.
    """
    sidecars = [sc for p in split.parts if (sc := sidecar_mod.read(p)) is not None]
    merged = Sidecar(
        store_url=_first(sc.store_url for sc in sidecars),
        bandcamp=_first(sc.bandcamp for sc in sidecars),
        downloaded_at=_earliest(sc.downloaded_at for sc in sidecars),
        # The album has existed since the first of its discs did.
        added_at=_earliest(sc.added_at for sc in sidecars) or datetime.now(UTC),
        mb_release_id=split.mb_release_id,
        # Last tagged is when the album — all of it — was last brought up to
        # date; a disc tagged earlier does not make the album older.
        tagged_at=_latest(sc.tagged_at for sc in sidecars),
        notes=_first(sc.notes for sc in sidecars),
        purchase_unavailable=any(sc.purchase_unavailable for sc in sidecars),
    )
    sidecar_mod.write(split.parent, merged)
    # NO alias row, and that is not an omission. #16 assumed one was needed —
    # an absorbed directory's id normally stops naming anything on disk, which
    # is exactly what `album_aliases` exists for (#33). It doesn't arise here:
    # an album's id IS its `mb_release_id` once it has one (`_album_id_of`), and
    # detection only groups parts that already agree on that release. So every
    # part and the parent share one id throughout, and the history recorded
    # against "CD2" is already reachable from the album that survives.
    audit.record(
        "album.group",
        album_id=split.mb_release_id,
        album=split.parent,
        release=split.mb_release_id,
        parts=len(split.parts),
        tracks=split.track_count,
    )
    return merged


def _first[T](values: Iterable[T | None]) -> T | None:
    """The first value that is set, or None. The parts describe one album, so
    "any of them knows" is the honest reading of a field only some carry."""
    return next((v for v in values if v is not None), None)


def _earliest(values: Iterable[datetime | None]) -> datetime | None:
    present = [v for v in values if v is not None]
    return min(present) if present else None


def _latest(values: Iterable[datetime | None]) -> datetime | None:
    present = [v for v in values if v is not None]
    return max(present) if present else None
