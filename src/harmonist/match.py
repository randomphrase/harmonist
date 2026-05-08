"""Confidence assessment between local files and an MB release.

Used by the sync orchestrator before tagging. If `assess_match` returns
"exact", the orchestrator promotes the candidate MBID to `mb_release_id`
and runs the tagger. Otherwise the candidate is stashed in
`mb_match_candidate` and the album waits in NEEDS_CONFIRMATION until the
user clicks Confirm or Reject.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from mutagen.mp4 import MP4

from .models import MatchCandidate, MatchConfidence, TrackComparison
from .tagger import _flatten_tracks, _track_title


# Per-track length tolerance. Anything within this is "close enough" — covers
# small encoder differences, gapless playback edits, etc. Anything beyond
# requires user confirmation.
LENGTH_TOLERANCE_MS = 4000


def assess_match(album_dir: Path, release: dict) -> MatchCandidate:
    """Compare files in album_dir to the MB release; return a MatchCandidate.

    Confidence levels:
      - "exact": file count == track count AND every per-track length is
        within LENGTH_TOLERANCE_MS of MB's recorded length AND no track has
        an unknown MB length.
      - "approximate": file count matches but at least one track is unknown
        or out of tolerance.
      - "no_match": file count differs from MB track count.
    """
    files = sorted(p for p in album_dir.glob("*.m4a") if p.is_file())
    tracks = list(_flatten_tracks(release))

    file_count = len(files)
    track_count = len(tracks)
    notes: list[str] = []

    if file_count != track_count:
        return MatchCandidate(
            mb_release_id=release["id"],
            confidence="no_match",
            file_count=file_count,
            track_count=track_count,
            track_comparisons=[],
            proposed_at=datetime.now(timezone.utc),
            notes=[
                f"file count {file_count} does not match MB track count {track_count}"
            ],
        )

    comparisons: list[TrackComparison] = []
    any_significant_delta = False
    any_unknown_length = False

    for f, (_, _, track) in zip(files, tracks):
        file_dur_ms = _file_duration_ms(f)
        mb_len_ms = _mb_track_length_ms(track)
        delta_ms = abs(file_dur_ms - mb_len_ms) if mb_len_ms is not None else None

        if mb_len_ms is None:
            any_unknown_length = True
        elif delta_ms is not None and delta_ms > LENGTH_TOLERANCE_MS:
            any_significant_delta = True

        comparisons.append(
            TrackComparison(
                file_name=f.name,
                file_duration_ms=file_dur_ms,
                mb_track_title=_track_title(track),
                mb_track_length_ms=mb_len_ms,
                delta_ms=delta_ms,
            )
        )

    if any_significant_delta:
        notes.append(
            f"some track lengths differ by more than {LENGTH_TOLERANCE_MS // 1000}s"
        )
    if any_unknown_length:
        notes.append("some MB tracks have no recorded length")

    confidence: MatchConfidence
    if not any_significant_delta and not any_unknown_length:
        confidence = "exact"
    else:
        confidence = "approximate"

    return MatchCandidate(
        mb_release_id=release["id"],
        confidence=confidence,
        file_count=file_count,
        track_count=track_count,
        track_comparisons=comparisons,
        proposed_at=datetime.now(timezone.utc),
        notes=notes,
    )


def _file_duration_ms(file_path: Path) -> int:
    try:
        audio = MP4(file_path)
        seconds = audio.info.length
        return int(seconds * 1000)
    except Exception:
        return 0


def _mb_track_length_ms(track: dict) -> int | None:
    """Pull the track length in milliseconds out of an MB track dict."""
    raw = (track.get("recording") or {}).get("length") or track.get("length")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None
