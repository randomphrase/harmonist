"""Confidence assessment between local files and an MB release.

Used by the sync orchestrator before tagging. If `assess_match` returns
"exact", the orchestrator promotes the candidate MBID to `mb_release_id`
and runs the tagger. Otherwise the candidate is stashed in
`mb_match_candidate` and the album stays in NEEDS_MBID — its card shows
the suggestion inline until the user Confirms or Dismisses it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import album_files, compare, formats
from .compare import LENGTH_TOLERANCE_MS
from .models import MatchCandidate, MatchConfidence, Release, Track, TrackComparison
from .tagger import _flatten_tracks, _identity_of, _track_title

__all__ = ["LENGTH_TOLERANCE_MS", "Ranking", "assess_match", "match_releases", "mb_track_lengths"]


def assess_match(album_dir: Path, release: Release) -> MatchCandidate:
    """Compare files in album_dir to the MB release; return a MatchCandidate.

    Confidence levels:
      - "exact": file count == track count AND every per-track length is
        within LENGTH_TOLERANCE_MS of MB's recorded length AND no track has
        an unknown MB length.
      - "approximate": file count matches but at least one track is unknown
        or out of tolerance.
      - "no_match": file count differs from MB track count.

    Which file is which track is decided by `compare.assign` (#395) — the same
    ladder the album page and the tagger use, so the three cannot answer it
    differently (#232). This used to pair the two lists **positionally**, which
    is only the ladder's bottom rung and reaches it without asking the files
    anything.

    *cv313 — live [w/japan exclusive album]* is what that cost. Bandcamp names
    a file `<track artist> - <album> - NN <title>`, and the credits vary across
    that release — `cv313`, `deepchord`, `echospace` — so the prefix decided
    the sort and the number in the middle of the name never got a vote. Ten
    correctly-numbered files whose durations matched MusicBrainz to the
    millisecond were compared against each other's tracks: nine rows of
    differences, a verdict of "approximate" instead of "exact", and an album
    parked in NEEDS_MBID waiting on a decision it never needed.

    So the deltas here are not merely displayed. They are what the confidence
    is derived from, and `match_releases` ranks releases on their total — a
    mis-pairing can pick the wrong release, not just describe one badly.
    """
    files = album_files.audio_files(album_dir)
    tracks = list(_flatten_tracks(release))

    file_count = len(files)
    track_count = len(tracks)
    notes: list[str] = []

    # One open per file, where the durations and titles alone took two. The
    # ladder needs the numbers and the release track id from the same read.
    tags = [formats.read_tags(f) for f in files]
    slots = compare.assign(
        [compare.identity_of(t) for t in tags],
        [_identity_of(t) for t in tracks],
    )
    file_of = {slot: i for i, slot in enumerate(slots) if slot is not None}

    # Rows run down the RELEASE's tracklist — so the panel numbers them the way
    # MusicBrainz does — and then whatever files found no slot in it. Both
    # halves are padded, because a row with nothing on one side is a finding
    # worth showing: a track that isn't on disk, a file the release has no
    # place for.
    rows: list[tuple[int | None, int | None]] = [(file_of.get(t), t) for t in range(track_count)]
    rows += [(f, None) for f, slot in enumerate(slots) if slot is None]

    comparisons: list[TrackComparison] = []
    any_significant_delta = False
    any_unknown_length = False

    for file_i, track_i in rows:
        track = tracks[track_i][2] if track_i is not None else None

        if file_i is not None:
            file_name = files[file_i].name
            # `or 0` as reading the duration on its own did: a file Harmonist
            # cannot open has no length, and calling that a match would be a
            # claim it never made.
            file_dur_ms = tags[file_i].duration_ms or 0
            file_title = tags[file_i].title or files[file_i].stem
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


@dataclass(frozen=True)
class Ranking:
    """How a set of candidate releases came out — and whether the ranking could
    actually tell them apart (#426).

    The second half is the point. Ranking picks a winner from any list, and
    `max()` picks the first of equals, so a set of releases the evidence cannot
    separate still produced a confident-looking answer whose identity was
    decided by MusicBrainz's response order. Two editions of one release, same
    tracklist, same durations, one Bandcamp URL: whichever came back first got
    written into the user's files.

    So the winner and the fact that it *is* one travel together, and a caller
    about to write tags has to read both. Not stored anywhere — this is the
    shape of an answer, not of a record.
    """

    #: Every candidate assessed, best first.
    candidates: tuple[MatchCandidate, ...]

    @property
    def best(self) -> MatchCandidate:
        return self.candidates[0]

    @property
    def equal_best(self) -> tuple[MatchCandidate, ...]:
        """`best` and every candidate level with it. One entry when the evidence
        names a single release; more when it merely fits several equally, which
        is a question for the user rather than an answer."""
        top = _rank_key(self.candidates[0])
        return tuple(c for c in self.candidates if _rank_key(c) == top)

    @property
    def unique(self) -> bool:
        """Whether `best` outranks everything else — the precondition for acting
        on it without asking. Matching is exact, scoped and *unique*, and this
        is the third of those."""
        return len(self.equal_best) == 1


def match_releases(album_dir: Path, releases: list[Release]) -> Ranking | None:
    """Rank the MB releases by how well their tracklists fit the files on disk.

    A single Bandcamp URL can map to several MB releases (see
    ``mb_lookup.lookup_by_bandcamp_url``). Assess the album against each and
    return them ordered, or None when ``releases`` is empty.

    Ranking, best first:
      1. confidence — exact > approximate > no_match;
      2. closer file/track counts (smaller ``|file_count - track_count|``);
      3. smaller total per-track length delta.

    For a 6-track download offered against a [1-track, 6-track] pair, the
    6-track release scores "exact" and the 1-track one "no_match", so the
    right release wins outright. The count/length tie-breakers only decide
    genuinely close calls.

    Returns a `Ranking` rather than the winner alone because those three
    criteria can run out — two editions of the same release fit identically —
    and the winner alone cannot say so (#426). Callers that act on the result
    check `unique` first.
    """
    if not releases:
        return None
    candidates = [assess_match(album_dir, r) for r in releases]
    # `sorted` is stable, so equals keep their input order — which is exactly
    # the order that must not be allowed to decide anything, and `unique` is
    # what stops it.
    return Ranking(tuple(sorted(candidates, key=_rank_key, reverse=True)))


_CONFIDENCE_RANK = {"exact": 2, "approximate": 1, "no_match": 0}


def _rank_key(c: MatchCandidate) -> tuple[int, int, int]:
    """Sort key for ``match_releases`` — higher is better, so the ranking sorts
    on it in reverse."""
    confidence = _CONFIDENCE_RANK[c.confidence]
    count_gap = abs(c.file_count - c.track_count)
    total_delta = sum(abs(tc.delta_ms) for tc in c.track_comparisons if tc.delta_ms is not None)
    return (confidence, -count_gap, -total_delta)


def mb_track_lengths(release: Release) -> list[int | None]:
    """Every track's length in ms, in the same order as `tagger.tagsets_for`.

    The tracklist comparison (#135) needs lengths beside the TagSets, and a
    length is not a tag — nothing writes it to a file — so it can't ride along
    inside `TagSet`. Sharing `_flatten_tracks` with `tagsets_for` is what
    guarantees the two lists line up; zipping two independently-ordered
    sequences would silently compare each file against the wrong track.
    """
    return [_mb_track_length_ms(track) for _, _, track in _flatten_tracks(release)]


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
