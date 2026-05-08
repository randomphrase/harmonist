"""Bootstrap an existing tagged music library.

Two phases, run in sequence:

Phase A — derive sidecars from existing MBID tags (always, no network):
  Walk the music dir; for any album with a `MusicBrainz Album Id` atom on its
  files but no `.harmonist.json` sidecar, write a sidecar with source="manual"
  and the MBID from the tag. Scanner now sees the album as Done, not Orphan.

Phase B — reconcile with Bandcamp purchases (only when cookies.txt is present):
  Load the user's Bandcamp purchase list (no downloads), look up each
  purchase's MBID via MB URL relationships, and for purchases that match an
  on-disk album, upgrade the sidecar to source="bandcamp" AND append the
  item_id to bandcampsync's ignores.txt so a subsequent Sync doesn't try to
  re-download it.

Both phases are idempotent — running twice is safe.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from mutagen.mp4 import MP4

from . import sidecar as sidecar_mod
from .bandcamp_hook import construct_bandcamp_url
from .mb_lookup import MBError, lookup_by_bandcamp_url
from .models import BandcampInfo, Sidecar
from .scanner import _find_album_dirs
from .tagger import ATOM_MB_ALBUM_ID


log = logging.getLogger(__name__)


@dataclass
class PhaseAStats:
    derived: int = 0
    skipped_existing: int = 0
    skipped_untagged: int = 0


@dataclass
class PhaseBStats:
    matched: int = 0
    unmatched_purchases: int = 0
    skipped_already_ignored: int = 0
    failed_lookups: int = 0


@dataclass
class BootstrapResult:
    phase_a: PhaseAStats = field(default_factory=PhaseAStats)
    phase_b: PhaseBStats | None = None  # None when cookies.txt not present
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Phase A
# ---------------------------------------------------------------------------


def derive_sidecars_from_tags(music_dir: Path, *, dry_run: bool = False) -> PhaseAStats:
    """Write source=manual sidecars for tagged albums that don't have one yet.

    Idempotent: skips albums that already have a sidecar.
    """
    stats = PhaseAStats()
    for album_dir, files in _find_album_dirs(music_dir):
        if sidecar_mod.has_sidecar(album_dir):
            stats.skipped_existing += 1
            continue
        mbid = _read_album_mbid(files)
        if not mbid:
            stats.skipped_untagged += 1
            continue
        sc = Sidecar(
            schema_version=1,
            source="manual",
            mb_release_id=mbid,
            added_at=datetime.now(timezone.utc),
            tagged_at=datetime.now(timezone.utc),
        )
        if not dry_run:
            sidecar_mod.write(album_dir, sc)
        stats.derived += 1
    return stats


def _read_album_mbid(m4a_files: list[Path]) -> str | None:
    """Return the first MusicBrainz Album Id atom found across the files."""
    for f in m4a_files:
        try:
            audio = MP4(f)
        except Exception:
            continue
        atom = audio.get(ATOM_MB_ALBUM_ID)
        if not atom:
            continue
        try:
            return atom[0].decode("utf-8")
        except (AttributeError, UnicodeDecodeError):
            continue
    return None


# ---------------------------------------------------------------------------
# Phase B
# ---------------------------------------------------------------------------


def reconcile_with_bandcamp_purchases(
    music_dir: Path,
    cookies_path: Path,
    ignores_path: Path,
    *,
    dry_run: bool = False,
    bandcamp_factory=None,
    lookup_fn=None,
) -> PhaseBStats:
    """Cross-reference Bandcamp purchases with on-disk MBIDs.

    For each purchase whose MBID (looked up via MB URL relationship) matches an
    on-disk sidecar, upgrade the sidecar to source="bandcamp" and append the
    item_id to ignores.txt.

    `bandcamp_factory` and `lookup_fn` are dependency injection points so tests
    don't need real Bandcamp credentials or real MB lookups.
    """
    stats = PhaseBStats()

    if bandcamp_factory is None:
        bandcamp_factory = _default_bandcamp_factory
    if lookup_fn is None:
        lookup_fn = lookup_by_bandcamp_url

    # Build mbid → (album_dir, sidecar) index for fast matching
    mbid_index: dict[str, tuple[Path, Sidecar]] = {}
    for album_dir, _files in _find_album_dirs(music_dir):
        sc = sidecar_mod.read(album_dir)
        if sc and sc.mb_release_id:
            mbid_index[sc.mb_release_id] = (album_dir, sc)

    bandcamp = bandcamp_factory(cookies_path)
    purchases = bandcamp.purchases

    existing_ignored = _read_existing_ignored_ids(ignores_path)
    new_ignores: list[tuple[int, str]] = []  # (item_id, comment)

    for item in purchases:
        url = construct_bandcamp_url(item)
        if not url:
            continue

        try:
            mbid = lookup_fn(url)
        except MBError as e:
            log.warning("bootstrap: MB lookup failed for %s: %s", url, e)
            stats.failed_lookups += 1
            continue

        if not mbid:
            stats.unmatched_purchases += 1
            continue

        match = mbid_index.get(mbid)
        if not match:
            stats.unmatched_purchases += 1
            continue

        album_dir, sc = match
        if not dry_run:
            _upgrade_sidecar_to_bandcamp(album_dir, sc, item, url)

        item_id = int(item.item_id)
        if item_id in existing_ignored:
            stats.skipped_already_ignored += 1
        else:
            comment = f"{getattr(item, 'band_name', '?')} / {getattr(item, 'item_title', '?')}"
            new_ignores.append((item_id, comment))
            existing_ignored.add(item_id)
            stats.matched += 1

    if new_ignores and not dry_run:
        _append_to_ignores(ignores_path, new_ignores)

    return stats


def _upgrade_sidecar_to_bandcamp(album_dir: Path, sc: Sidecar, item, url: str) -> None:
    """Rewrite the sidecar with source=bandcamp + bandcamp block."""
    band_id_raw = getattr(item, "_data", {}).get("band_id")
    band_id = int(band_id_raw) if band_id_raw is not None else None

    upgraded = Sidecar(
        schema_version=sc.schema_version,
        source="bandcamp",
        bandcamp=BandcampInfo(url=url, item_id=int(item.item_id), band_id=band_id),
        downloaded_at=sc.downloaded_at or sc.added_at,
        added_at=sc.added_at,
        mb_release_id=sc.mb_release_id,
        mb_match_candidate=sc.mb_match_candidate,
        mb_last_checked_at=sc.mb_last_checked_at,
        mb_lookup_history=sc.mb_lookup_history,
        tagged_at=sc.tagged_at,
        notes=sc.notes,
    )
    sidecar_mod.write(album_dir, upgraded)


# ---------------------------------------------------------------------------
# ignores.txt manipulation
# ---------------------------------------------------------------------------

_IGNORES_DELIMITER = (
    "# IDs of items already downloaded will be automatically added below this line.\n"
    "# =========================================================\n"
)


def _read_existing_ignored_ids(ignores_path: Path) -> set[int]:
    """Parse the existing ignores.txt and return the set of item_ids already in it."""
    if not ignores_path.exists():
        return set()
    ids: set[int] = set()
    for raw in ignores_path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            ids.add(int(line))
        except ValueError:
            continue
    return ids


def _append_to_ignores(ignores_path: Path, new_entries: list[tuple[int, str]]) -> None:
    """Append entries below the auto-managed delimiter, atomically.

    If the file doesn't exist, create it with the delimiter at the top.
    If it exists but has no delimiter, append one.
    """
    ignores_path.parent.mkdir(parents=True, exist_ok=True)

    existing = ""
    if ignores_path.exists():
        existing = ignores_path.read_text(encoding="utf-8")
    if "# IDs of items already downloaded" not in existing:
        if existing and not existing.endswith("\n"):
            existing += "\n"
        existing += "\n" + _IGNORES_DELIMITER

    new_lines = "".join(f"{item_id}  # {comment}\n" for item_id, comment in new_entries)
    payload = existing + new_lines

    tmp = ignores_path.with_suffix(ignores_path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(ignores_path)


def _default_bandcamp_factory(cookies_path: Path):
    """Build a real bandcampsync.Bandcamp client from a cookies.txt path."""
    from bandcampsync.bandcamp import Bandcamp

    cookies = cookies_path.read_text(encoding="utf-8")
    bc = Bandcamp(cookies=cookies)
    bc.verify_authentication()
    bc.load_purchases()
    return bc


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def bootstrap(
    music_dir: Path,
    *,
    cookies_path: Path | None = None,
    ignores_path: Path | None = None,
    dry_run: bool = False,
    bandcamp_factory=None,
    lookup_fn=None,
) -> BootstrapResult:
    """Run the full bootstrap. Phase B is skipped if cookies.txt is missing."""
    result = BootstrapResult()
    result.phase_a = derive_sidecars_from_tags(music_dir, dry_run=dry_run)

    if cookies_path and cookies_path.exists() and ignores_path is not None:
        try:
            result.phase_b = reconcile_with_bandcamp_purchases(
                music_dir,
                cookies_path,
                ignores_path,
                dry_run=dry_run,
                bandcamp_factory=bandcamp_factory,
                lookup_fn=lookup_fn,
            )
        except Exception as e:
            log.exception("bootstrap phase B failed")
            result.errors.append(f"Phase B: {e}")

    return result
