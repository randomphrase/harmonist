"""Confidence assessment between local files and an MB release.

Used by the sync orchestrator before tagging. If `assess_match` returns
"exact", the orchestrator promotes the candidate MBID to `mb_release_id`
and runs the tagger. Otherwise the candidate is stashed in
`mb_match_candidate` and the album waits in NEEDS_REVIEW until the
user clicks Confirm or Reject.
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import zip_longest
from pathlib import Path

from . import formats
from .models import MatchCandidate, MatchConfidence, Release, Track, TrackComparison
from .tagger import _flatten_tracks, _track_title

# Per-track length tolerance. Anything within this is "close enough" — covers
# small encoder differences, gapless playback edits, etc. Anything beyond
# requires user confirmation.
LENGTH_TOLERANCE_MS = 4000


def assess_match(album_dir: Path, release: Release) -> MatchCandidate:
    """Compare files in album_dir to the MB release; return a MatchCandidate.

    Confidence levels:
      - "exact": file count == track count AND every per-track length is
        within LENGTH_TOLERANCE_MS of MB's recorded length AND no track has
        an unknown MB length.
      - "approximate": file count matches but at least one track is unknown
        or out of tolerance.
      - "no_match": file count differs from MB track count.
    """
    files = sorted(p for p in album_dir.iterdir() if formats.is_supported(p))
    tracks = list(_flatten_tracks(release))

    file_count = len(files)
    track_count = len(tracks)
    notes: list[str] = []

    comparisons: list[TrackComparison] = []
    any_significant_delta = False
    any_unknown_length = False

    for f, mb_entry in zip_longest(files, tracks, fillvalue=None):
        track = mb_entry[2] if mb_entry is not None else None

        if f is not None:
            file_name = f.name
            file_dur_ms = _file_duration_ms(f)
            file_title = _file_title(f) or f.stem
        else:
            file_name = None
            file_dur_ms = None
            file_title = None

        if track is not None:
            mb_track_title = _track_title(track)
            mb_len_ms = _mb_track_length_ms(track)
        else:
            mb_track_title = None
            mb_len_ms = None

        if file_dur_ms is not None and mb_len_ms is not None:
            delta_ms = abs(file_dur_ms - mb_len_ms)
            if delta_ms > LENGTH_TOLERANCE_MS:
                any_significant_delta = True
        else:
            delta_ms = None
            if track is not None and mb_len_ms is None:
                any_unknown_length = True

        comparisons.append(
            TrackComparison(
                file_name=file_name,
                file_duration_ms=file_dur_ms,
                file_title=file_title,
                mb_track_title=mb_track_title,
                mb_track_length_ms=mb_len_ms,
                delta_ms=delta_ms,
            )
        )

    confidence: MatchConfidence
    if file_count != track_count:
        confidence = "no_match"
        notes.append(f"file count {file_count} does not match MB track count {track_count}")
    elif any_significant_delta:
        confidence = "approximate"
        notes.append(f"some track lengths differ by more than {LENGTH_TOLERANCE_MS // 1000}s")
        if any_unknown_length:
            notes.append("some MB tracks have no recorded length")
    elif any_unknown_length:
        confidence = "approximate"
        notes.append("some MB tracks have no recorded length")
    else:
        confidence = "exact"

    return MatchCandidate(
        mb_release_id=release["id"],
        confidence=confidence,
        file_count=file_count,
        track_count=track_count,
        track_comparisons=comparisons,
        proposed_at=datetime.now(UTC),
        notes=notes,
    )


def _file_duration_ms(file_path: Path) -> int:
    return formats.read_duration_ms(file_path) or 0


def _file_title(file_path: Path) -> str | None:
    """Read the track title tag from the file, if present."""
    return formats.read_track_title(file_path)


def _mb_track_length_ms(track: Track) -> int | None:
    """Pull the track length in milliseconds out of an MB track dict.

    Prefer the per-release *track* length (what the release page shows and
    what the audio actually is) over the *recording* length, which is a
    property of the shared recording entity and can differ by several
    seconds across releases.
    """
    raw = track.get("length") or (track.get("recording") or {}).get("length")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None
