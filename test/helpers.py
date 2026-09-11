"""Shared fixture builders for the test suite.

A module rather than `conftest.py` so plain helper functions can be imported by
name — conftest is loaded by pytest, not importable as `conftest` from a test.
"""

from __future__ import annotations

from pathlib import Path


def keep_one(data: bytes, *, mime: str | None = None) -> str | None:
    """Keep one image in the artwork store, returning its digest if it was
    retained — the one-image case of `artwork_store.keep_all`, for tests that
    are about the store rather than about an operation overwriting images."""
    from harmonist import artwork_store

    key = artwork_store.digest(data)
    return key if key in artwork_store.keep_all([(data, mime)]) else None


def write_track_totals(
    album_dir: Path,
    *,
    track_total: int,
    disc_num: int = 1,
    disc_total: int = 1,
    pattern: str = "*.m4a",
) -> None:
    """Write the `trkn` / `disk` totals onto an album's files.

    This is how an album is made INCOMPLETE in a test since #195: the expected
    track count is read from the files' own tags, exactly as a real tagging (or
    Picard) leaves them, so a fixture that wants "MusicBrainz says 4 tracks and
    only 1 is here" says so in the tags rather than in a sidecar field.
    """
    from mutagen.mp4 import MP4

    for i, f in enumerate(sorted(album_dir.glob(pattern)), start=1):
        audio = MP4(f)
        audio["trkn"] = [(i, track_total)]
        audio["disk"] = [(disc_num, disc_total)]
        audio.save()
