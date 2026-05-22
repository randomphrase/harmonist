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

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

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
        return direct

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


def write_sidecar_for_item(item: Any, album_dir: Path) -> bool:
    """Write or update the sidecar for a Bandcamp item at album_dir.

    If a sidecar already exists (typical after reconciliation has run), fills
    in the missing `bandcamp.item_id` / `band_id`. Otherwise creates a fresh
    sidecar for a brand-new download.

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

    existing = sidecar_mod.read(album_dir)
    if existing is not None:
        # Reconciliation produced a sidecar earlier; fill in what's missing.
        merged_bandcamp = BandcampInfo(
            item_id=item_id,
            band_id=band_id
            if band_id is not None
            else (existing.bandcamp.band_id if existing.bandcamp else None),
        )
        merged = Sidecar(
            schema_version=existing.schema_version,
            store_url=existing.store_url or url,  # keep existing canonical URL
            bandcamp=merged_bandcamp,
            downloaded_at=existing.downloaded_at or datetime.now(timezone.utc),
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
        bandcamp=BandcampInfo(item_id=item_id, band_id=band_id),
        downloaded_at=datetime.now(timezone.utc),
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


class HarmonistSyncer(_BCSyncer):
    """bandcampsync.Syncer subclass with download cap + sidecar capture.

    NOTE: bandcampsync's parent __init__ runs the sync eagerly (calls
    asyncio.run(self.sync_items()) before returning). Our overrides hook into
    that flow: sync_items() pre-checks the cap, sync_item() post-writes the
    sidecar after each successful download.

    All arguments are keyword-only. `dir_path` is foolproofed: accepts either
    a `str` or `Path` and coerces to `Path` before handing to bandcampsync,
    whose `LocalMedia` uses Path-only operations (`.iterdir()`, `/`).
    """

    def __init__(
        self,
        *,
        dir_path: "Path | str",
        max_downloads_per_sync: int,
        progress_callback: "Optional[Callable[[str], None]]" = None,
        **kwargs: Any,
    ):
        self._max_downloads_per_sync = max_downloads_per_sync
        self._progress_callback = progress_callback
        super().__init__(dir_path=Path(dir_path), **kwargs)

    async def sync_items(self):
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

    def sync_item(self, item):
        if self._progress_callback:
            label = f"{getattr(item, 'band_name', '?')} / {getattr(item, 'item_title', '?')}"
            try:
                self._progress_callback(label)
            except Exception:
                pass  # never let a progress callback failure abort the sync

        # Short-circuit: if reconciliation has already created a sidecar
        # for this Bandcamp URL elsewhere on disk, don't re-download. Just
        # fill in the item_id and append to ignores.txt.
        url = construct_bandcamp_url(item)
        media_dir = getattr(self.local_media, "media_dir", None)
        if url and media_dir:
            existing_dir = find_existing_album_by_url(Path(media_dir), url)
            if existing_dir is not None:
                try:
                    write_sidecar_for_item(item, existing_dir)
                    self.ignores.add(item)
                except Exception as e:
                    log.warning(
                        "could not fill in pre-existing sidecar for %s: %s",
                        getattr(item, "item_id", "?"),
                        e,
                    )
                return False  # didn't download (already on disk)

        result = super().sync_item(item)
        if result:
            local_path = self.local_media.get_path_for_purchase(item)
            try:
                write_sidecar_for_item(item, local_path)
            except Exception as e:
                log.warning(
                    "post-download sidecar write failed for item %s: %s",
                    getattr(item, "item_id", "?"),
                    e,
                )
        return result
