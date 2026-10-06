"""Upgrade the archive cache location without moving artwork undo backups."""

from __future__ import annotations

import logging
import stat
from pathlib import Path

from . import activity, activity_store, audit

log = logging.getLogger(__name__)


def migrate_artwork_cache(legacy: Path, destination: Path) -> None:
    """Move the whole cache before workers start; a restart is a no-op.

    One directory rename preserves image bytes and LRU timestamps without a
    partially copied store or migration marker. Existing destinations and
    symlinked legacy directories require an explicit resolution, never a merge
    that could overwrite an image. Undo backups stay in their original place.
    """
    with activity_store.action():
        try:
            try:
                old = legacy.lstat()
            except FileNotFoundError:
                return  # Fresh install, or the atomic move already completed.
            if not stat.S_ISDIR(old.st_mode):
                raise OSError(f"The legacy cache {legacy} is not a regular directory")
            try:
                destination.lstat()
            except FileNotFoundError:
                pass  # A missing destination is required, including no dangling symlink.
            else:
                raise FileExistsError(f"The destination {destination} already exists")
            audit.record("artwork.cache_move", source=legacy, destination=destination)
            legacy.rename(destination)
        except OSError as exc:
            message = (
                f"Artwork cache migration from {legacy} to {destination} failed: {exc}. "
                "Resolve the path conflict or permissions and restart; "
                "no cache or artwork backup files were deleted."
            )
            log.exception(message)
            raise RuntimeError(message) from exc
        activity.record(f"Artwork cache moved to {destination}")
