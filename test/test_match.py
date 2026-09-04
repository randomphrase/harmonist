"""Tests for match.assess_match — confidence + per-track deltas."""

from __future__ import annotations

import shutil
from pathlib import Path

from harmonist.match import _mb_track_length_ms, assess_match, best_match
from harmonist.models import MatchCandidate, TrackComparison
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


def test_track_length_prefers_track_over_recording():
    """Per-release track length wins over the recording's own length — they
    can differ by seconds (real example: MB release 02ba70f3, track 6 is
    6:32 as a track but 6:26 as a recording). Reading the recording value
    caused a phantom delta that tripped NEEDS_REVIEW."""
    track = {
        "length": "392581",  # 6:32 — the per-release track time
        "recording": {"id": "rec", "length": "386000"},  # 6:26 — recording time
    }
    assert _mb_track_length_ms(track) == 392581


def test_track_length_falls_back_to_recording():
    track = {"recording": {"id": "rec", "length": "386000"}}
    assert _mb_track_length_ms(track) == 386000


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
    track = rel["medium-list"][0]["track-list"][0]
    # The per-release track title is authoritative and must win over a differing
    # recording title (issue #27) — the assessment display would otherwise show
    # the stale recording name.
    track["title"] = "Song A"
    track["recording"]["title"] = "Song A /w Someone"
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


# ---------- best_match: one Bandcamp URL → several MB releases ----------


def _release_with_id(rel_id: str, track_lengths_ms: list[int | None]) -> dict:
    rel = _release(track_lengths_ms)
    rel["id"] = rel_id
    return rel


def test_best_match_none_when_no_releases(tmp_path):
    assert best_match(_album_with(tmp_path, 1), []) is None


def test_best_match_picks_tracklist_that_fits_the_files(tmp_path):
    """The Variant case: a 6-track digital edition and a 1-track CD mix share
    one Bandcamp URL. A 6-file download must resolve to the 6-track release —
    the 1-track release is a clean no_match and must lose."""
    album_dir = _album_with(tmp_path, 6)
    cd_mix = _release_with_id("rel-cd", [FIXTURE_DURATION_MS])
    digital = _release_with_id("rel-digital", [FIXTURE_DURATION_MS] * 6)

    result = best_match(album_dir, [cd_mix, digital])
    assert result is not None
    assert result.mb_release_id == "rel-digital"
    assert result.confidence == "exact"


def test_best_match_picks_single_track_release_for_single_file(tmp_path):
    """Mirror of the above: the 1-file CD-mix download resolves to the
    1-track release, not the 6-track one. Order is independent of input."""
    album_dir = _album_with(tmp_path, 1)
    cd_mix = _release_with_id("rel-cd", [FIXTURE_DURATION_MS])
    digital = _release_with_id("rel-digital", [FIXTURE_DURATION_MS] * 6)

    result = best_match(album_dir, [digital, cd_mix])
    assert result is not None
    assert result.mb_release_id == "rel-cd"
    assert result.confidence == "exact"


def test_best_match_breaks_count_ties_on_closest_lengths(tmp_path):
    """Two releases with the same track count but different lengths: the one
    whose track lengths sit closest to the files wins, even when neither is a
    perfect exact match."""
    album_dir = _album_with(tmp_path, 1)
    close = _release_with_id("rel-close", [FIXTURE_DURATION_MS + 10_000])  # +10s
    far = _release_with_id("rel-far", [FIXTURE_DURATION_MS + 60_000])  # +60s

    result = best_match(album_dir, [far, close])
    assert result is not None
    assert result.mb_release_id == "rel-close"


# -- title-discrepancy signal (issue #29) --


def _tc(file_title: str | None, mb_title: str | None) -> TrackComparison:
    return TrackComparison(
        file_name="x.m4a",
        file_duration_ms=1000,
        file_title=file_title,
        mb_track_title=mb_title,
        mb_track_length_ms=1000,
        delta_ms=0,
    )


def test_title_differs_flags_real_difference():
    assert _tc("Ground Glass [w/ Foxes in Fiction]", "Ground Glass").title_differs is True


def test_title_differs_ignores_case_and_whitespace():
    assert _tc("Ground  Glass", "ground glass").title_differs is False


def test_title_differs_false_when_a_side_is_missing():
    # Padding rows (count mismatch) are not a metadata discrepancy.
    assert _tc(None, "Ground Glass").title_differs is False
    assert _tc("Ground Glass", None).title_differs is False


def test_title_mismatch_count_sums_differing_rows():
    cand = MatchCandidate(
        mb_release_id="rel",
        confidence="exact",
        file_count=3,
        track_count=3,
        track_comparisons=[
            _tc("Same", "Same"),
            _tc("Ground Glass [w/ Foxes in Fiction]", "Ground Glass"),
            _tc("Etalon [w/ Foxes in Fiction]", "Etalon"),
        ],
    )
    assert cand.title_mismatch_count == 2


def test_title_mismatch_count_zero_on_clean_match():
    cand = MatchCandidate(
        mb_release_id="rel",
        confidence="exact",
        file_count=1,
        track_count=1,
        track_comparisons=[_tc("Same Title", "Same Title")],
    )
    assert cand.title_mismatch_count == 0


# -- typographic variants of one mark (#379) --


def test_title_differs_ignores_a_mark_respelt_in_another_typeface():
    """One punctuation mark in two spellings is not a difference in the title.

    The case that raised it: an adopted album whose files carry the ASCII
    apostrophe MusicBrainz spells with U+2019 — one character, in one track, and
    the whole album read as a retitle.

    Each pair below is a different *class* of respelling rather than another
    character, because a class is what `norm_title` claims to handle: quotes,
    dashes, a decomposed accent, the full-width forms a CJK title arrives in, the
    ellipsis, the non-breaking space.

    Every pair is asserted unequal before it is compared, and that guard is not
    ceremony: three of these rows differ by a character that is INVISIBLE in the
    source — a combining accent, a non-breaking space, a full-width bracket. An
    editor, a paste, or a well-meant reformat can flatten one of them into a
    string compared with itself, which would pass here while testing nothing.
    """
    for on_disk, from_mb in [
        ("Humanity's Shadow", "Humanity’s Shadow"),  # apostrophe
        ('He Said "Hi"', "He Said “Hi”"),  # double quotes
        ("Blue - Green", "Blue — Green"),  # hyphen vs em dash
        ("Café Noir", "Café Noir"),  # decomposed vs composed accent
        ("夜(よる)", "夜（よる）"),  # full-width brackets
        ("Wait...", "Wait…"),  # ellipsis
        ("Rock and Roll", "Rock and Roll"),  # non-breaking space
    ]:
        assert on_disk != from_mb, on_disk
        assert _tc(on_disk, from_mb).title_differs is False, on_disk


def test_title_differs_still_sees_punctuation_that_says_something():
    """Marks are canonicalised, never dropped — so a title that genuinely gained,
    lost or changed one is still a difference.

    The direction matters more than the cases: understating a retitle is what
    `owned.BY_VALUE` warns about, and a rule that stripped punctuation instead of
    folding it would do exactly that.
    """
    for on_disk, from_mb in [
        ("Live?", "Live!"),
        ("Ground Glass", "Ground Glass?"),
        ("Rock and Roll", "Rock & Roll"),
        ("Dawn Chorus", "Dawn Chorus (Alt. Take)"),
    ]:
        assert _tc(on_disk, from_mb).title_differs is True, on_disk
