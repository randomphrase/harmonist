"""Tests for match.assess_match — confidence + per-track deltas."""

from __future__ import annotations

import shutil
from pathlib import Path

from harmonist.match import assess_match
from harmonist.tagger import ATOM_TITLE

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SINE_M4A = FIXTURES_DIR / "sine.m4a"

# The sine.m4a fixture is exactly 1 second long → 1000 ms.
FIXTURE_DURATION_MS = 1000


def _album_with(tmp_path: Path, n: int) -> Path:
    d = tmp_path / "Artist" / "Album"
    d.mkdir(parents=True)
    for i in range(1, n + 1):
        shutil.copy(SINE_M4A, d / f"{i:02d} Track {i}.m4a")
    return d


def _release(track_lengths_ms: list[int | None]) -> dict:
    """Build an MB release dict with given track lengths (None = unknown)."""
    tracks = []
    for i, length in enumerate(track_lengths_ms, start=1):
        track: dict = {
            "id": f"rt-{i}",
            "position": str(i),
            "title": f"Track {i}",
            "recording": {"id": f"rec-{i}", "title": f"Track {i}"},
        }
        if length is not None:
            track["recording"]["length"] = str(length)
        tracks.append(track)
    return {
        "id": "rel-aaa",
        "title": "Test Album",
        "medium-list": [{"position": "1", "track-list": tracks}],
    }


# ---------- exact ----------


def test_exact_when_count_and_lengths_match(tmp_path):
    album_dir = _album_with(tmp_path, 2)
    rel = _release([FIXTURE_DURATION_MS, FIXTURE_DURATION_MS])
    result = assess_match(album_dir, rel)
    assert result.confidence == "exact"
    assert result.file_count == 2
    assert result.track_count == 2
    assert result.mb_release_id == "rel-aaa"
    assert result.notes == []
    assert len(result.track_comparisons) == 2
    for tc in result.track_comparisons:
        assert tc.delta_ms == 0


def test_exact_within_tolerance(tmp_path):
    album_dir = _album_with(tmp_path, 1)
    # Off by 3 seconds — under 4s tolerance
    rel = _release([FIXTURE_DURATION_MS + 3000])
    result = assess_match(album_dir, rel)
    assert result.confidence == "exact"


# ---------- approximate ----------


def test_approximate_when_one_track_outside_tolerance(tmp_path):
    album_dir = _album_with(tmp_path, 2)
    # Second track off by 10 seconds — way over tolerance
    rel = _release([FIXTURE_DURATION_MS, FIXTURE_DURATION_MS + 10000])
    result = assess_match(album_dir, rel)
    assert result.confidence == "approximate"
    assert result.file_count == 2
    assert result.track_count == 2
    assert "differ by more than" in result.notes[0]
    assert result.track_comparisons[0].delta_ms == 0
    assert result.track_comparisons[1].delta_ms == 10000


def test_approximate_when_mb_length_unknown(tmp_path):
    album_dir = _album_with(tmp_path, 1)
    rel = _release([None])  # MB has no length
    result = assess_match(album_dir, rel)
    assert result.confidence == "approximate"
    assert any("no recorded length" in n for n in result.notes)
    tc = result.track_comparisons[0]
    assert tc.mb_track_length_ms is None
    assert tc.delta_ms is None


def test_approximate_with_mixed_known_and_unknown(tmp_path):
    album_dir = _album_with(tmp_path, 3)
    rel = _release([FIXTURE_DURATION_MS, None, FIXTURE_DURATION_MS])
    result = assess_match(album_dir, rel)
    # Counts match, lengths within tolerance where known, but one unknown
    assert result.confidence == "approximate"
    assert any("no recorded length" in n for n in result.notes)


# ---------- no match ----------


def test_no_match_when_more_files_than_tracks(tmp_path):
    album_dir = _album_with(tmp_path, 3)
    rel = _release([FIXTURE_DURATION_MS])
    result = assess_match(album_dir, rel)
    assert result.confidence == "no_match"
    assert result.file_count == 3
    assert result.track_count == 1
    # Side-by-side is padded with the longer side, MB-side null for extras
    assert len(result.track_comparisons) == 3
    assert result.track_comparisons[0].file_name == "01 Track 1.m4a"
    assert result.track_comparisons[0].mb_track_title == "Track 1"
    assert result.track_comparisons[1].file_name == "02 Track 2.m4a"
    assert result.track_comparisons[1].mb_track_title is None
    assert result.track_comparisons[1].mb_track_length_ms is None
    assert result.track_comparisons[2].file_name == "03 Track 3.m4a"
    assert result.track_comparisons[2].mb_track_title is None
    assert "does not match" in result.notes[0]


def test_no_match_when_fewer_files_than_tracks(tmp_path):
    album_dir = _album_with(tmp_path, 1)
    rel = _release([FIXTURE_DURATION_MS, FIXTURE_DURATION_MS])
    result = assess_match(album_dir, rel)
    assert result.confidence == "no_match"
    assert result.file_count == 1
    assert result.track_count == 2
    # Side-by-side padded; the 2nd row has no file
    assert len(result.track_comparisons) == 2
    assert result.track_comparisons[0].file_name == "01 Track 1.m4a"
    assert result.track_comparisons[0].mb_track_title == "Track 1"
    assert result.track_comparisons[1].file_name is None
    assert result.track_comparisons[1].file_duration_ms is None
    assert result.track_comparisons[1].mb_track_title == "Track 2"


# ---------- candidate metadata ----------


def test_candidate_carries_release_mbid(tmp_path):
    album_dir = _album_with(tmp_path, 1)
    rel = _release([FIXTURE_DURATION_MS])
    rel["id"] = "rel-zzz"
    result = assess_match(album_dir, rel)
    assert result.mb_release_id == "rel-zzz"


def test_candidate_records_proposed_at(tmp_path):
    album_dir = _album_with(tmp_path, 1)
    result = assess_match(album_dir, _release([FIXTURE_DURATION_MS]))
    assert result.proposed_at is not None


def test_track_comparison_has_file_and_mb_titles(tmp_path):
    album_dir = _album_with(tmp_path, 1)
    rel = _release([FIXTURE_DURATION_MS])
    rel["medium-list"][0]["track-list"][0]["recording"]["title"] = "Song A"
    result = assess_match(album_dir, rel)
    tc = result.track_comparisons[0]
    assert tc.mb_track_title == "Song A"
    assert tc.file_name == "01 Track 1.m4a"
    assert tc.file_duration_ms == FIXTURE_DURATION_MS


def test_track_comparison_reads_file_title_from_tag(tmp_path):
    """If the file has a ©nam tag, file_title should be the tag value."""
    from mutagen.mp4 import MP4

    album_dir = _album_with(tmp_path, 1)
    audio = MP4(album_dir / "01 Track 1.m4a")
    audio[ATOM_TITLE] = ["The Real Title"]
    audio.save()

    result = assess_match(album_dir, _release([FIXTURE_DURATION_MS]))
    assert result.track_comparisons[0].file_title == "The Real Title"


def test_track_comparison_falls_back_to_filename_stem(tmp_path):
    """When no ©nam tag, file_title falls back to the filename stem."""
    album_dir = _album_with(tmp_path, 1)
    # sine.m4a fixture has no ©nam by default
    result = assess_match(album_dir, _release([FIXTURE_DURATION_MS]))
    assert result.track_comparisons[0].file_title == "01 Track 1"


# ---------- empty album ----------


def test_no_match_when_no_files(tmp_path):
    album_dir = tmp_path / "Empty"
    album_dir.mkdir()
    result = assess_match(album_dir, _release([FIXTURE_DURATION_MS]))
    assert result.confidence == "no_match"
    assert result.file_count == 0
