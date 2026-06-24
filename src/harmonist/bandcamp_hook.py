"""Subclass of bandcampsync.Syncer that captures Bandcamp URLs into sidecars.

Two added behaviours on top of the parent:
  1. Hard cap on per-sync download count, checked BEFORE any download starts.
  2. After each successful download, write a `.harmonist.json` sidecar in the
     album directory capturing the public Bandcamp album URL.

The cap is the safety mechanism per design §11.3 — it protects the user from
a misconfigured ignores file accidentally re-downloading their whole
collection.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from bandcampsync.options import BandcampSyncOptions
from bandcampsync.sync import Syncer as _BCSyncer

from . import activity
from . import sidecar as sidecar_mod
from .models import BandcampInfo, Sidecar
from .sidecar import CURRENT_SCHEMA_VERSION

log = logging.getLogger(__name__)


class CapExceededError(Exception):
    """Raised when a sync would download more items than the configured cap."""


def construct_bandcamp_url(item: Any) -> str | None:
    """Construct the public Bandcamp album URL from a BandcampItem.

    Tries direct `item_url` first, then composes from `url_hints` (which is
    documented in the bandcampsync source — see bandcamp.py). Returns None if
    we can't determine the URL.
    """
    if direct := getattr(item, "_data", {}).get("item_url"):
        return str(direct)

    hints = getattr(item, "_data", {}).get("url_hints")
    if not isinstance(hints, dict):
        return None

    slug = hints.get("slug")
    if not slug:
        return None
    item_type = hints.get("item_type", "album")

    if custom := hints.get("custom_domain"):
        return f"https://{custom}/{item_type}/{slug}"
    if subdomain := hints.get("subdomain"):
        return f"https://{subdomain}.bandcamp.com/{item_type}/{slug}"
    return None


def check_download_cap(would_download_count: int, cap: int) -> None:
    """Raise CapExceededError if the count exceeds the cap. Equal is OK."""
    if would_download_count > cap:
        raise CapExceededError(
            f"sync would download {would_download_count} items, "
            f"exceeds cap of {cap} — refusing to proceed"
        )


def write_sidecar_for_item(item: Any, album_dir: Path, *, prefer_item_url: bool = False) -> bool:
    """Write or update the sidecar for a Bandcamp item at album_dir.

    If a sidecar already exists (typical after reconciliation has run), fills
    in the missing `bandcamp.item_id` / `band_id`. Otherwise creates a fresh
    sidecar for a brand-new download.

    `prefer_item_url`: normally we keep an existing sidecar's `store_url`
    (it's the canonical MB-derived URL). But when we matched the item to this
    album by *slug* — i.e. the existing URL and the item URL point at the same
    release under different subdomains — the item's URL is where the user
    actually purchased it, so we adopt it as the authoritative store_url.

    Returns True on success, False if the URL couldn't be reconstructed.
    """
    url = construct_bandcamp_url(item)
    if not url:
        log.warning(
            "could not construct Bandcamp URL for item %s — skipping sidecar",
            getattr(item, "item_id", "?"),
        )
        return False

    band_id_raw = getattr(item, "_data", {}).get("band_id")
    band_id = int(band_id_raw) if band_id_raw is not None else None
    item_id = int(item.item_id)
    is_private = bool(getattr(item, "_data", {}).get("is_private"))

    existing = sidecar_mod.read(album_dir)
    if existing is not None:
        # Reconciliation produced a sidecar earlier; fill in what's missing.
        merged_bandcamp = BandcampInfo(
            item_id=item_id,
            band_id=band_id
            if band_id is not None
            else (existing.bandcamp.band_id if existing.bandcamp else None),
            is_private=is_private,
        )
        merged = Sidecar(
            schema_version=existing.schema_version,
            # Keep the existing canonical URL, unless a slug match told us to
            # adopt the item's (purchase-authoritative) URL instead.
            store_url=url if prefer_item_url else (existing.store_url or url),
            bandcamp=merged_bandcamp,
            downloaded_at=existing.downloaded_at or datetime.now(UTC),
            added_at=existing.added_at,
            mb_release_id=existing.mb_release_id,
            mb_match_candidate=existing.mb_match_candidate,
            tagged_at=existing.tagged_at,
            notes=existing.notes,
        )
        sidecar_mod.write(album_dir, merged)
        return True

    sc = Sidecar(
        schema_version=CURRENT_SCHEMA_VERSION,
        store_url=url,
        bandcamp=BandcampInfo(item_id=item_id, band_id=band_id, is_private=is_private),
        downloaded_at=datetime.now(UTC),
    )
    sidecar_mod.write(album_dir, sc)
    return True


def write_ambiguous_candidates(album_dir: Path, item_ids: list[int]) -> bool:
    """Record the set of purchase ids this album *could* be — when several
    editions share one store URL and a title tiebreak couldn't separate them.

    Leaves `item_id` unset (we don't know which) but stores the candidates, so
    the scanner treats the album as resolved-enough (out of NEEDS_SYNC) rather
    than nagging forever. Only annotates an existing (reconciled) sidecar.
    """
    existing = sidecar_mod.read(album_dir)
    if existing is None:
        return False
    bc = existing.bandcamp
    merged_bc = BandcampInfo(
        item_id=None,
        band_id=bc.band_id if bc else None,
        is_private=bc.is_private if bc else False,
        candidate_item_ids=sorted({int(i) for i in item_ids}),
    )
    merged = Sidecar(
        schema_version=existing.schema_version,
        store_url=existing.store_url,
        bandcamp=merged_bc,
        downloaded_at=existing.downloaded_at,
        added_at=existing.added_at,
        mb_release_id=existing.mb_release_id,
        mb_match_candidate=existing.mb_match_candidate,
        tagged_at=existing.tagged_at,
        notes=existing.notes,
        track_count_expected=existing.track_count_expected,
    )
    sidecar_mod.write(album_dir, merged)
    return True


def _norm_title(s: str) -> str:
    """Lowercase + keep only alphanumerics — for an exact (not fuzzy) title
    compare between an album folder name and a purchase title. Exact-only keeps
    the tiebreak safe: a near-but-not-equal title falls through to ambiguous
    rather than risking a mis-link."""
    return "".join(ch for ch in s.lower() if ch.isalnum())


def find_existing_album_by_url(music_dir: Path, target_url: str) -> Path | None:
    """Scan music_dir for a sidecar whose store_url matches target_url.

    Used at sync time to short-circuit re-downloading albums that already
    exist on disk (post-reconciliation).
    """
    for f in music_dir.rglob(".harmonist.json"):
        try:
            sc = sidecar_mod.read(f.parent)
        except Exception:
            continue
        if sc and sc.store_url == target_url:
            return f.parent
    return None


def survey_album_links(music_dir: Path) -> tuple[dict[str, list[Path]], set[int]]:
    """One pass over music_dir → `({release slug: [unlinked album dirs]},
    {item_ids already linked to some album})`.

    Built once per sync so the ignored-purchase backfill is O(albums + ignored)
    rather than re-scanning the whole library for every purchase.

    - The slug map holds only *unlinked* albums, so a backfill can only ever
      *fill in* a missing id — never hijack a correctly-linked album.
    - The linked-id set lets the backfill skip a purchase that's already tied to
      a different album: two editions can share a store_url slug (a standard +
      a long-form edition whose MB release only carries the public page URL), so
      slug alone would otherwise attach the standard edition's purchase to the
      still-unlinked long-form album. Skipping already-linked purchases prevents
      that mis-link.
    """
    by_slug: dict[str, list[Path]] = {}
    linked_ids: set[int] = set()
    for f in music_dir.rglob(".harmonist.json"):
        try:
            sc = sidecar_mod.read(f.parent)
        except Exception:
            continue
        if sc is None or not sc.store_url:
            continue
        if sc.bandcamp is not None and sc.bandcamp.item_id is not None:
            linked_ids.add(int(sc.bandcamp.item_id))
            continue  # already linked
        if slug := album_slug(sc.store_url):
            by_slug.setdefault(slug, []).append(f.parent)
    return by_slug, linked_ids


def album_slug(url: str | None) -> str | None:
    """Extract the Bandcamp release slug from a URL, ignoring the subdomain.

    `https://echospace313.bandcamp.com/album/dimensional-space-remastered-by-pole`
    → `album/dimensional-space-remastered-by-pole`

    The slug is Bandcamp's stable per-release handle: minted once at release
    and effectively immutable, even when the artist renames the band or
    re-letters the title. The *subdomain*, by contrast, varies — the same
    release is often cross-listed under both a label page and an artist page
    (e.g. `echospacedetroit` vs the artist's own subdomain), which defeats a
    whole-URL compare. Matching on the slug bridges that.

    Returns `None` for URLs that aren't an `/album/<slug>` or `/track/<slug>`
    shape (e.g. a bare `artist.bandcamp.com` landing page embedded in tags) —
    those carry no release identity and must never match.

    The item-type segment (`album`/`track`) is kept in the key so an album and
    a track that happen to share a slug don't collide.
    """
    if not url:
        return None
    try:
        path = urlparse(url).path
    except ValueError:
        return None
    parts = [seg for seg in path.split("/") if seg]
    if len(parts) >= 2 and parts[-2] in ("album", "track"):
        return f"{parts[-2]}/{parts[-1].lower()}"
    return None


def find_existing_album_by_slug(music_dir: Path, target_url: str) -> Path | None:
    """Slug-match fallback for `find_existing_album_by_url`.

    Find an on-disk album whose `store_url` shares `target_url`'s release slug
    (subdomain ignored) AND which isn't already linked to a Bandcamp item.
    Restricting to unlinked albums (no `bandcamp.item_id`) means we only ever
    *fill in* a missing id, never hijack a correctly-linked album.

    Guards against ambiguity: if two or more unlinked albums share the slug we
    return `None` and leave them for manual linking rather than guess. Returns
    `None` when `target_url` has no slug (see `album_slug`).
    """
    target = album_slug(target_url)
    if target is None:
        return None
    matches: list[Path] = []
    for f in music_dir.rglob(".harmonist.json"):
        try:
            sc = sidecar_mod.read(f.parent)
        except Exception:
            continue
        if sc is None or not sc.store_url:
            continue
        if sc.bandcamp is not None and sc.bandcamp.item_id is not None:
            continue  # already linked — don't touch
        if album_slug(sc.store_url) == target:
            matches.append(f.parent)
    return matches[0] if len(matches) == 1 else None


# bandcampsync ships no types, so _BCSyncer is Any; subclassing it is the
# whole point of this module.
class HarmonistSyncer(_BCSyncer):  # type: ignore[misc]
    """bandcampsync.Syncer subclass with download cap + sidecar capture.

    NOTE: when ``auto_run`` is true (the default), bandcampsync's parent
    __init__ runs the sync eagerly (calls asyncio.run(self.sync_items())
    before returning). Our overrides hook into that flow: sync_items()
    pre-checks the cap, sync_item() post-writes the sidecar after each
    successful download.

    This is the adapter layer over bandcampsync: it takes our flat keyword
    args and assembles the ``BandcampSyncOptions`` the 0.8 ``Syncer`` wants,
    so the rest of Harmonist never imports bandcampsync's option model.
    ``dir_path`` is foolproofed (str or Path → Path) because bandcampsync's
    ``LocalMedia`` uses Path-only operations (`.iterdir()`, `/`).
    """

    def __init__(
        self,
        *,
        cookies: str,
        dir_path: Path | str,
        media_format: str,
        max_downloads_per_sync: int,
        temp_dir_root: Path | None = None,
        ign_file_path: str | Path | None = None,
        ign_patterns: str = "",
        notify_url: str | None = None,
        progress_callback: Callable[[str], None] | None = None,
        post_download_callback: Callable[[Path], None] | None = None,
        auto_run: bool = True,
    ):
        self._max_downloads_per_sync = max_downloads_per_sync
        self._progress_callback = progress_callback
        # Called with the album dir after each successful download + sidecar
        # write. Used to auto-resolve the store URL against MusicBrainz so an
        # in-MB release skips NEEDS_MBID. Must never abort the sync.
        self._post_download_callback = post_download_callback
        # Count of albums actually downloaded this run (the sync runner reports
        # it). Set before super().__init__ because bandcampsync runs the whole
        # sync eagerly inside __init__.
        self.new_items = 0
        options = BandcampSyncOptions(
            cookies=cookies,
            dir_path=Path(dir_path),
            media_format=media_format,
            temp_dir_root=temp_dir_root,
            ign_file_path=Path(ign_file_path) if ign_file_path is not None else None,
            ign_patterns=ign_patterns,
            notify_url=notify_url,
        )
        super().__init__(options, auto_run=auto_run)

    async def sync_items(self) -> None:
        # Link already-downloaded (ignored) purchases to their on-disk albums
        # BEFORE the parent loop — bandcampsync skips ignored items, so their
        # sidecar would otherwise never get its item_id filled in.
        self._backfill_ignored_purchases()
        # Count items that would actually download (i.e. not ignored, not preorder).
        candidates = []
        for item in self.bandcamp.purchases:
            if self.ignores.is_ignored(item):
                continue
            if getattr(item, "is_preorder", False):
                continue
            candidates.append(item)
        check_download_cap(len(candidates), self._max_downloads_per_sync)
        await super().sync_items()

    def _backfill_ignored_purchases(self) -> None:
        """Link already-downloaded (ignored) purchases to their on-disk albums.

        bandcampsync's loop skips ignored items before `sync_item` runs, so an
        album downloaded once (now ignored) can otherwise never get its item_id
        — it stays stuck in NEEDS_SYNC forever. Linking metadata is independent
        of downloading, so we do it here.

        Two phases:
          1. per store-URL slug (`_resolve_slug_group`): a clean 1:1 links
             directly; several editions sharing one store URL are separated by a
             title tiebreak; an unbreakable tie is recorded as an *ambiguous*
             link (candidate ids) so it leaves NEEDS_SYNC rather than nagging.
          2. title fallback: an album still unlinked (its store_url matched no
             purchase, e.g. the long-form edition whose MB-recorded URL is the
             *public* page but whose actual purchase has its own URL) is linked
             by a unique exact-title match against the remaining unmatched
             purchases — across the URL mismatch.
        """
        media_dir = getattr(self.local_media, "media_dir", None)
        if not media_dir:
            return
        by_slug, linked_ids = survey_album_links(Path(media_dir))
        stuck_total = sum(len(v) for v in by_slug.values())
        if not by_slug:
            return

        # Candidate purchases: ignored (so sync_item skips them) and not already
        # linked to some album (the guard against attaching a standard edition's
        # purchase to an unlinked sibling that shares a slug).
        candidates: list[Any] = []
        ignored_total = 0
        for item in self.bandcamp.purchases:
            if not self.ignores.is_ignored(item):
                continue  # non-ignored items are handled by sync_item's own path
            ignored_total += 1
            item_id_raw = getattr(item, "item_id", None)
            if item_id_raw is not None and int(item_id_raw) in linked_ids:
                continue
            candidates.append(item)

        purchases_by_slug: dict[str, list[Any]] = {}
        for item in candidates:
            slug = album_slug(construct_bandcamp_url(item))
            if slug:
                purchases_by_slug.setdefault(slug, []).append(item)

        # Phase 1: per store-URL slug.
        consumed: set[int] = set()
        linked = 0
        ambiguous = 0
        unlinked: list[Path] = []  # albums to try by title in phase 2
        for slug, albums in by_slug.items():
            purchases = purchases_by_slug.get(slug, [])
            if not purchases:
                unlinked.extend(albums)  # slug matched no purchase
                continue
            g_linked, g_ambiguous, g_unlinked = self._resolve_slug_group(
                albums, purchases, consumed
            )
            linked += g_linked
            ambiguous += g_ambiguous
            unlinked.extend(g_unlinked)

        # Phase 2: title fallback across the URL mismatch. Index the still-
        # unmatched purchases by normalized title; link an album to the one
        # whose title uniquely matches its folder name.
        title_index: dict[str, list[Any]] = {}
        for p in candidates:
            if int(p.item_id) in consumed:
                continue
            title_index.setdefault(_norm_title(getattr(p, "item_title", "")), []).append(p)
        title_linked = 0
        for album_dir in unlinked:
            avail = [
                p
                for p in title_index.get(_norm_title(album_dir.name), [])
                if int(p.item_id) not in consumed
            ]
            if len(avail) == 1:
                # Capture the album's store_url (its tagged release's URL) BEFORE
                # linking — _link adopts the purchase URL (prefer_item_url=True).
                existing = sidecar_mod.read(album_dir)
                store_url = existing.store_url if existing else None
                purchase_url = construct_bandcamp_url(avail[0])
                self._link(album_dir, avail[0])
                consumed.add(int(avail[0].item_id))
                title_linked += 1
                # A title link ALWAYS has a URL mismatch (that's why it fell out
                # of the slug pass). The tagged release's store URL not matching
                # the purchase is a possible mis-tag — but it can also be a
                # correctly-tagged edition whose MB URL is the shared public page
                # (we can't tell apart without comparing tracklists). Warn so it's
                # reviewable; it links regardless. WARNING → also the Activity feed.
                log.warning(
                    "Linked %r to a purchase by title (item_id=%s). Possible mis-tag: "
                    "the tagged release's store URL (%s) differs from the matched "
                    "purchase URL (%s) — this can be a correctly-tagged edition whose "
                    "MB URL is the shared public page, or the wrong release.",
                    album_dir.name,
                    getattr(avail[0], "item_id", "?"),
                    store_url or "?",
                    purchase_url or "?",
                )

        log.info(
            "Backfill: %d purchase(s) loaded (%d ignored); %d unlinked album(s) on disk; "
            "linked %d by URL + %d by title, marked %d ambiguous",
            len(self.bandcamp.purchases),
            ignored_total,
            stuck_total,
            linked,
            title_linked,
            ambiguous,
        )

    def _resolve_slug_group(
        self, albums: list[Path], purchases: list[Any], consumed: set[int]
    ) -> tuple[int, int, list[Path]]:
        """Link the on-disk `albums` and `purchases` that all share one store-URL
        slug. Records linked purchase ids into `consumed`. Returns
        (linked, ambiguous, still_unlinked) — the last being albums that couldn't
        be resolved AND had no leftover purchase to mark ambiguous, so the caller
        retries them by title.

        - 1 album + 1 purchase → link directly (unambiguous).
        - otherwise → pair by an exact normalized title match (folder name vs
          purchase title); link unique winners, then a final 1-album/1-purchase
          remainder by elimination.
        - leftover purchases the title couldn't separate → ambiguous (store the
          candidate ids). No leftover purchase → return the album for phase 2.
        """
        purchases = list(purchases)

        def _take(album_dir: Path, item: Any) -> None:
            self._link(album_dir, item)
            consumed.add(int(item.item_id))

        if len(albums) == 1 and len(purchases) == 1:
            _take(albums[0], purchases[0])
            return (1, 0, [])

        linked = 0
        unpaired: list[Path] = []
        for album_dir in albums:
            key = _norm_title(album_dir.name)
            matches = [p for p in purchases if _norm_title(getattr(p, "item_title", "")) == key]
            if len(matches) == 1:
                _take(album_dir, matches[0])
                purchases.remove(matches[0])
                linked += 1
            else:
                unpaired.append(album_dir)

        # Elimination: a lone album + lone purchase left over must be each other.
        if len(unpaired) == 1 and len(purchases) == 1:
            _take(unpaired[0], purchases[0])
            return (linked + 1, 0, [])

        # No purchase left for this slug → hand the unpaired albums to phase 2.
        candidates = [p for p in purchases if getattr(p, "item_id", None) is not None]
        cand_ids = [int(p.item_id) for p in candidates]
        if not cand_ids:
            return (linked, 0, unpaired)

        # Leftover purchases the title couldn't separate → ambiguous.
        cand_desc = ", ".join(
            f"{int(p.item_id)} ({getattr(p, 'item_title', '?')})" for p in candidates
        )
        ambiguous = 0
        for album_dir in unpaired:
            try:
                if write_ambiguous_candidates(album_dir, cand_ids):
                    ambiguous += 1
                    log.warning(
                        "Ambiguous Bandcamp link for %r: could be item_id %s — "
                        "%d editions share this store URL and the title didn't "
                        "single one out. Stored all candidates; left out of Needs Sync.",
                        album_dir.name,
                        cand_desc,
                        len(cand_ids),
                    )
            except Exception as e:
                log.warning("could not mark %s ambiguous: %s", album_dir.name, e)
        return (linked, ambiguous, [])

    def _link(self, album_dir: Path, item: Any) -> None:
        try:
            if write_sidecar_for_item(item, album_dir, prefer_item_url=True):
                # A linked album leaves Needs Sync for the Library. Record the
                # transition in the Activity feed (and server log).
                activity.record(
                    f"{getattr(item, 'band_name', '?')} — {getattr(item, 'item_title', '?')}: "
                    f"Needs Sync → Library (linked to Bandcamp purchase "
                    f"{getattr(item, 'item_id', '?')})"
                )
        except Exception as e:
            log.warning(
                "could not backfill ignored purchase %s: %s",
                getattr(item, "item_id", "?"),
                e,
            )

    def sync_item(self, item: Any, encoding: str | None = None) -> bool:
        if self._progress_callback:
            label = f"{getattr(item, 'band_name', '?')} / {getattr(item, 'item_title', '?')}"
            # Never let a progress callback failure abort the sync.
            with contextlib.suppress(Exception):
                self._progress_callback(label)

        # Short-circuit: if reconciliation has already created a sidecar
        # for this Bandcamp URL elsewhere on disk, don't re-download. Just
        # fill in the item_id and append to ignores.txt.
        url = construct_bandcamp_url(item)
        media_dir = getattr(self.local_media, "media_dir", None)
        if url and media_dir:
            # Exact-URL match first; fall back to a subdomain-agnostic slug
            # match (same release cross-listed under a different subdomain).
            # The slug fallback adopts the item's URL as the new store_url.
            existing_dir = find_existing_album_by_url(Path(media_dir), url)
            by_slug = False
            if existing_dir is None:
                existing_dir = find_existing_album_by_slug(Path(media_dir), url)
                by_slug = existing_dir is not None
            if existing_dir is not None:
                try:
                    write_sidecar_for_item(item, existing_dir, prefer_item_url=by_slug)
                    if by_slug:
                        log.info(
                            "Linked by slug: %s / %s → %s (item_id=%s)",
                            getattr(item, "band_name", "?"),
                            getattr(item, "item_title", "?"),
                            existing_dir.name,
                            getattr(item, "item_id", "?"),
                        )
                    # Guard the add: this short-circuit runs every sync for an
                    # on-disk album, and bandcampsync's Ignores.add appends a
                    # line unconditionally (no dedup) — so re-adding bloats
                    # ignores.txt with duplicates. Only mark it once.
                    if not self.ignores.is_ignored(item):
                        self.ignores.add(item)
                except Exception as e:
                    log.warning(
                        "could not fill in pre-existing sidecar for %s: %s",
                        getattr(item, "item_id", "?"),
                        e,
                    )
                return False  # didn't download (already on disk)

        result = bool(super().sync_item(item, encoding))
        if result:
            self.new_items += 1
            local_path = self.local_media.get_path_for_purchase(item)
            try:
                write_sidecar_for_item(item, local_path)
            except Exception as e:
                log.warning(
                    "post-download sidecar write failed for item %s: %s",
                    getattr(item, "item_id", "?"),
                    e,
                )
            else:
                self._run_post_download(local_path)
        return result

    def _run_post_download(self, album_dir: Path) -> None:
        """Invoke the post-download hook (MB auto-resolve). Never aborts sync."""
        if self._post_download_callback is None:
            return
        with contextlib.suppress(Exception):
            self._post_download_callback(album_dir)

    def unmatched_purchases(self) -> list[tuple[str, str]]:
        """Purchases that linked to NO on-disk album this sync — their item_id
        appears in no sidecar. Returns ``(url, label)`` pairs; the caller cross-
        references these against unlinked on-disk albums (by MusicBrainz release
        group) to spot mis-tags. Best-effort: returns [] on any trouble."""
        media_dir = getattr(self.local_media, "media_dir", None)
        if not media_dir:
            return []
        linked: set[int] = set()
        for f in Path(media_dir).rglob(".harmonist.json"):
            try:
                sc = sidecar_mod.read(f.parent)
            except Exception:
                continue
            if sc and sc.bandcamp and sc.bandcamp.item_id is not None:
                linked.add(int(sc.bandcamp.item_id))
        out: list[tuple[str, str]] = []
        for item in self.bandcamp.purchases:
            try:
                iid = int(item.item_id)
            except (TypeError, ValueError):
                continue
            if iid in linked:
                continue
            url = construct_bandcamp_url(item)
            if url:
                label = f"{getattr(item, 'band_name', '?')} / {getattr(item, 'item_title', '?')}"
                out.append((url, label))
        return out
