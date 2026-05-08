"""Read/write .harmonist.json sidecars atomically."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .models import BandcampInfo, MatchCandidate, MBLookupAttempt, Sidecar, TrackComparison


SIDECAR_FILENAME = ".harmonist.json"
CURRENT_SCHEMA_VERSION = 1
MB_HISTORY_LIMIT = 10


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
    """Atomic: write to temp, fsync, rename."""
    target = sidecar_path(album_dir)
    tmp = target.with_suffix(target.suffix + ".tmp")
    payload = _to_dict(sidecar)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)


def _to_dict(s: Sidecar) -> dict:
    d: dict = {
        "schema_version": s.schema_version,
        "source": s.source,
    }
    if s.bandcamp:
        bd: dict = {"url": s.bandcamp.url, "item_id": s.bandcamp.item_id}
        if s.bandcamp.band_id is not None:
            bd["band_id"] = s.bandcamp.band_id
        d["bandcamp"] = bd
    if s.downloaded_at:
        d["downloaded_at"] = _iso(s.downloaded_at)
    if s.added_at:
        d["added_at"] = _iso(s.added_at)
    if s.mb_release_id:
        d["mb_release_id"] = s.mb_release_id
    if s.mb_match_candidate:
        d["mb_match_candidate"] = _candidate_to_dict(s.mb_match_candidate)
    if s.mb_last_checked_at:
        d["mb_last_checked_at"] = _iso(s.mb_last_checked_at)
    if s.mb_lookup_history:
        d["mb_lookup_history"] = [
            _attempt_to_dict(a) for a in s.mb_lookup_history[-MB_HISTORY_LIMIT:]
        ]
    if s.tagged_at:
        d["tagged_at"] = _iso(s.tagged_at)
    if s.notes is not None:
        d["notes"] = s.notes
    return d


def _attempt_to_dict(a: MBLookupAttempt) -> dict:
    out: dict = {"at": _iso(a.at), "result": a.result}
    if a.mbid:
        out["mbid"] = a.mbid
    if a.error:
        out["error"] = a.error
    return out


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
    out: dict = {
        "file_name": tc.file_name,
        "file_duration_ms": tc.file_duration_ms,
        "mb_track_title": tc.mb_track_title,
    }
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
                file_name=tc["file_name"],
                file_duration_ms=int(tc["file_duration_ms"]),
                mb_track_title=tc["mb_track_title"],
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
            f"sidecar at {source_path} has schema_version={sv}, expected {CURRENT_SCHEMA_VERSION}"
        )
    source = d.get("source")
    if source not in ("bandcamp", "manual"):
        raise InvalidSidecar(f"sidecar at {source_path} has invalid source: {source!r}")

    bandcamp = None
    if "bandcamp" in d:
        bd = d["bandcamp"]
        try:
            bandcamp = BandcampInfo(url=bd["url"], item_id=int(bd["item_id"]), band_id=bd.get("band_id"))
        except (KeyError, TypeError, ValueError) as e:
            raise InvalidSidecar(f"sidecar at {source_path} has malformed bandcamp block: {e}") from e

    candidate = None
    if "mb_match_candidate" in d:
        try:
            candidate = _candidate_from_dict(d["mb_match_candidate"])
        except (KeyError, TypeError, ValueError) as e:
            raise InvalidSidecar(
                f"sidecar at {source_path} has malformed mb_match_candidate: {e}"
            ) from e

    return Sidecar(
        schema_version=sv,
        source=source,
        bandcamp=bandcamp,
        downloaded_at=_parse_iso(d.get("downloaded_at")),
        added_at=_parse_iso(d.get("added_at")),
        mb_release_id=d.get("mb_release_id"),
        mb_match_candidate=candidate,
        mb_last_checked_at=_parse_iso(d.get("mb_last_checked_at")),
        mb_lookup_history=[
            MBLookupAttempt(
                at=_parse_iso(a["at"]),
                result=a["result"],
                mbid=a.get("mbid"),
                error=a.get("error"),
            )
            for a in d.get("mb_lookup_history", [])
        ],
        tagged_at=_parse_iso(d.get("tagged_at")),
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
