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


def unlinked_albums_by_slug(music_dir: Path) -> dict[str, list[Path]]:
    """One pass over music_dir → `{release slug: [album dirs]}` for albums that
    have a store_url but NO Bandcamp item_id yet (still unlinked).

    Built once per sync so the ignored-purchase backfill is O(albums + ignored)
    rather than re-scanning the whole library for every purchase. Only unlinked
    albums are included, so a backfill can only ever *fill in* a missing id —
    never hijack a correctly-linked album that happens to share a slug.
    """
    out: dict[str, list[Path]] = {}
    for f in music_dir.rglob(".harmonist.json"):
        try:
            sc = sidecar_mod.read(f.parent)
        except Exception:
            continue
        if sc is None or not sc.store_url:
            continue
        if sc.bandcamp is not None and sc.bandcamp.item_id is not None:
            continue  # already linked
        if slug := album_slug(sc.store_url):
            out.setdefault(slug, []).append(f.parent)
    return out


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
        """Fill in `item_id` for purchases already in `ignores.txt` whose
        on-disk album is still unlinked.

        bandcampsync's loop skips ignored items before `sync_item` runs, so an
        album downloaded once (now ignored) can otherwise never get its item_id
        — it stays stuck in NEEDS_SYNC forever. Linking metadata is independent
        of downloading, so we do it here for every ignored purchase.

        Slug-matched against the *unlinked* on-disk albums only (a single pass),
        and only when exactly one unlinked album shares the slug — so we never
        hijack a correctly-linked edition that happens to share the store URL
        (e.g. a standard + long-form edition on the same Bandcamp page).
        """
        media_dir = getattr(self.local_media, "media_dir", None)
        if not media_dir:
            return
        by_slug = unlinked_albums_by_slug(Path(media_dir))
        if not by_slug:
            return
        linked = 0
        for item in self.bandcamp.purchases:
            if not self.ignores.is_ignored(item):
                continue  # non-ignored items are handled by sync_item's own backfill
            slug = album_slug(construct_bandcamp_url(item))
            if not slug:
                continue
            dirs = by_slug.get(slug)
            if not dirs or len(dirs) != 1:
                continue  # 0 = not on disk / already linked; >1 = ambiguous, skip
            album_dir = dirs[0]
            try:
                if write_sidecar_for_item(item, album_dir, prefer_item_url=True):
                    linked += 1
                    # Don't let a second ignored item with the same slug relink
                    # the (now linked) dir.
                    del by_slug[slug]
                    log.info(
                        "Linked already-downloaded purchase by slug: %s / %s → %s (item_id=%s)",
                        getattr(item, "band_name", "?"),
                        getattr(item, "item_title", "?"),
                        album_dir.name,
                        getattr(item, "item_id", "?"),
                    )
            except Exception as e:
                log.warning(
                    "could not backfill ignored purchase %s: %s",
                    getattr(item, "item_id", "?"),
                    e,
                )
        if linked:
            log.info("Backfilled %d already-downloaded purchase(s) into existing albums", linked)

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
