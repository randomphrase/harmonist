"""Read/write .harmonist.json sidecars atomically."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path

from . import id_registry
from .models import BandcampInfo, MatchCandidate, Sidecar, TrackComparison


SIDECAR_FILENAME = ".harmonist.json"
CURRENT_SCHEMA_VERSION = 1


class UnsupportedSchemaVersion(Exception):
    pass


class InvalidSidecar(Exception):
    pass


def sidecar_path(album_dir: Path) -> Path:
    return album_dir / SIDECAR_FILENAME


def has_sidecar(album_dir: Path) -> bool:
    return sidecar_path(album_dir).exists()


def read(album_dir: Path) -> Sidecar | None:
    p = sidecar_path(album_dir)
    if not p.exists():
        return None
    with open(p, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise InvalidSidecar(f"sidecar at {p} is not valid JSON: {e}") from e
    return _from_dict(data, source_path=p)


def write(album_dir: Path, sidecar: Sidecar) -> None:
    """Atomic: write to temp, fsync, rename.

    Normalises identity at the persistence boundary so callers don't have
    to remember: if `mb_release_id` is set, drop any stale `temp_uid`;
    otherwise reuse the registry UUID for this path (if any) or mint a
    fresh one. Result: exactly one of `(mb_release_id, temp_uid)` is
    non-null on disk, and the URL stays the same across the NEW →
    sidecar'd transition.
    """
    sidecar = _normalise_identity(sidecar, album_dir)
    assert bool(sidecar.mb_release_id) ^ bool(sidecar.temp_uid), (
        f"sidecar identity invariant violated: mb_release_id="
        f"{sidecar.mb_release_id!r}, temp_uid={sidecar.temp_uid!r}"
    )
    target = sidecar_path(album_dir)
    tmp = target.with_suffix(target.suffix + ".tmp")
    payload = _to_dict(sidecar)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)


def _normalise_identity(s: Sidecar, album_dir: Path) -> Sidecar:
    """Enforce identity invariant: exactly one of (mb_release_id, temp_uid)
    is non-null. MBID always wins; temp_uid is minted iff there's no MBID.

    When minting, prefer the registry's UUID for this path (set by the
    scanner when it first saw a NEW album) so the inbox URL the user
    interacted with stays valid across the first sidecar write.
    """
    if s.mb_release_id:
        if s.temp_uid is None:
            return s
        return replace(s, temp_uid=None)
    if s.temp_uid is None:
        return replace(s, temp_uid=id_registry.peek(album_dir) or uuid.uuid4().hex)
    return s


def _to_dict(s: Sidecar) -> dict:
    d: dict = {"schema_version": s.schema_version}
    if s.store_url:
        d["store_url"] = s.store_url
    if s.bandcamp:
        bd: dict = {}
        if s.bandcamp.item_id is not None:
            bd["item_id"] = s.bandcamp.item_id
        if s.bandcamp.band_id is not None:
            bd["band_id"] = s.bandcamp.band_id
        if bd:  # only include the block when it has content
            d["bandcamp"] = bd
    if s.downloaded_at:
        d["downloaded_at"] = _iso(s.downloaded_at)
    if s.added_at:
        d["added_at"] = _iso(s.added_at)
    if s.mb_release_id:
        d["mb_release_id"] = s.mb_release_id
    if s.temp_uid:
        d["temp_uid"] = s.temp_uid
    if s.mb_match_candidate:
        d["mb_match_candidate"] = _candidate_to_dict(s.mb_match_candidate)
    if s.tagged_at:
        d["tagged_at"] = _iso(s.tagged_at)
    if s.track_count_expected is not None:
        d["track_count_expected"] = s.track_count_expected
    if s.notes is not None:
        d["notes"] = s.notes
    return d


def _candidate_to_dict(c: MatchCandidate) -> dict:
    out: dict = {
        "mb_release_id": c.mb_release_id,
        "confidence": c.confidence,
        "file_count": c.file_count,
        "track_count": c.track_count,
    }
    if c.track_comparisons:
        out["track_comparisons"] = [_comparison_to_dict(tc) for tc in c.track_comparisons]
    if c.proposed_at:
        out["proposed_at"] = _iso(c.proposed_at)
    if c.notes:
        out["notes"] = list(c.notes)
    return out


def _comparison_to_dict(tc: TrackComparison) -> dict:
    out: dict = {}
    if tc.file_name is not None:
        out["file_name"] = tc.file_name
    if tc.file_duration_ms is not None:
        out["file_duration_ms"] = tc.file_duration_ms
    if tc.file_title is not None:
        out["file_title"] = tc.file_title
    if tc.mb_track_title is not None:
        out["mb_track_title"] = tc.mb_track_title
    if tc.mb_track_length_ms is not None:
        out["mb_track_length_ms"] = tc.mb_track_length_ms
    if tc.delta_ms is not None:
        out["delta_ms"] = tc.delta_ms
    return out


def _candidate_from_dict(d: dict) -> MatchCandidate:
    return MatchCandidate(
        mb_release_id=d["mb_release_id"],
        confidence=d["confidence"],
        file_count=int(d["file_count"]),
        track_count=int(d["track_count"]),
        track_comparisons=[
            TrackComparison(
                file_name=tc.get("file_name"),
                file_duration_ms=tc.get("file_duration_ms"),
                file_title=tc.get("file_title"),
                mb_track_title=tc.get("mb_track_title"),
                mb_track_length_ms=tc.get("mb_track_length_ms"),
                delta_ms=tc.get("delta_ms"),
            )
            for tc in d.get("track_comparisons", [])
        ],
        proposed_at=_parse_iso(d.get("proposed_at")),
        notes=list(d.get("notes", [])),
    )


def _from_dict(d: dict, source_path: Path) -> Sidecar:
    sv = d.get("schema_version")
    if sv != CURRENT_SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            f"sidecar at {source_path} has schema_version={sv}, expected "
            f"{CURRENT_SCHEMA_VERSION}. Delete the sidecar and re-reconcile."
        )

    bandcamp = None
    if "bandcamp" in d:
        bd = d["bandcamp"]
        try:
            item_id_raw = bd.get("item_id")
            item_id = int(item_id_raw) if item_id_raw is not None else None
            bandcamp = BandcampInfo(item_id=item_id, band_id=bd.get("band_id"))
        except (KeyError, TypeError, ValueError) as e:
            raise InvalidSidecar(
                f"sidecar at {source_path} has malformed bandcamp block: {e}"
            ) from e

    candidate = None
    if "mb_match_candidate" in d:
        try:
            candidate = _candidate_from_dict(d["mb_match_candidate"])
        except (KeyError, TypeError, ValueError) as e:
            raise InvalidSidecar(
                f"sidecar at {source_path} has malformed mb_match_candidate: {e}"
            ) from e

    mb_release_id = d.get("mb_release_id")
    temp_uid = d.get("temp_uid")
    if mb_release_id and temp_uid:
        raise InvalidSidecar(
            f"sidecar at {source_path} has both mb_release_id and temp_uid "
            f"set; these are mutually exclusive."
        )

    return Sidecar(
        schema_version=sv,
        store_url=d.get("store_url"),
        bandcamp=bandcamp,
        downloaded_at=_parse_iso(d.get("downloaded_at")),
        added_at=_parse_iso(d.get("added_at")),
        mb_release_id=mb_release_id,
        temp_uid=temp_uid,
        mb_match_candidate=candidate,
        tagged_at=_parse_iso(d.get("tagged_at")),
        track_count_expected=d.get("track_count_expected"),
        notes=d.get("notes"),
    )


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str | None) -> datetime | None:
    if s is None:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)
