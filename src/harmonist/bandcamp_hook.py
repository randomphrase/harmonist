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
from typing import Any

from bandcampsync.sync import Syncer as _BCSyncer

from . import sidecar as sidecar_mod
from .models import BandcampInfo, Sidecar


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
    """Write a .harmonist.json sidecar for a freshly-downloaded Bandcamp item.

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

    sc = Sidecar(
        schema_version=1,
        source="bandcamp",
        bandcamp=BandcampInfo(
            url=url,
            item_id=int(item.item_id),
            band_id=band_id,
        ),
        downloaded_at=datetime.now(timezone.utc),
    )
    sidecar_mod.write(album_dir, sc)
    return True


class HarmonistSyncer(_BCSyncer):
    """bandcampsync.Syncer subclass with download cap + sidecar capture.

    NOTE: bandcampsync's parent __init__ runs the sync eagerly (calls
    asyncio.run(self.sync_items()) before returning). Our overrides hook into
    that flow: sync_items() pre-checks the cap, sync_item() post-writes the
    sidecar after each successful download.
    """

    def __init__(self, *args, max_downloads_per_sync: int, **kwargs):
        self._max_downloads_per_sync = max_downloads_per_sync
        super().__init__(*args, **kwargs)

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
