"""Tests for the disk-vs-MusicBrainz comparison model (#106).

Values throughout are real ones from a Bandcamp library, because the cases that
matter are the ones a synthetic "foo" vs "bar" never produces: separator
punctuation in an artist credit, a date MusicBrainz knows more precisely, a
featured credit MB keeps out of the title.
"""

from __future__ import annotations

from harmonist.compare import (
    Agreement,
    AlbumComparison,
    Consensus,
    Kind,
    compare_field,
    consensus,
    diff_runs,
)


def _text(runs) -> str:
    return "".join(r.text for r in runs)


def _changed(runs) -> list[str]:
    return [r.text for r in runs if r.changed]


# ---------- consensus across tracks ----------


def test_unanimous_tracks_give_the_album_its_value():
    c = consensus([(f"{i:02d}.flac", "Obreel") for i in range(1, 9)])
    assert c.value == "Obreel"
    assert c.is_unanimous and c.agreeing == 8 and c.total == 8
    assert c.outliers == ()


def test_a_majority_wins_and_names_the_outliers():
    """A field on six of eight tracks IS what the album says — with two tracks
    to point at, not an absent value."""
    tracks: list[tuple[str, str | None]] = [
        (f"{i:02d}.flac", "Galán | Spieth | Guentner") for i in range(1, 7)
    ]
    tracks += [("07 Fernwald.mp3", "Benoît Pioulard"), ("08 Halde.mp3", None)]
    c = consensus(tracks)
    assert c.value == "Galán | Spieth | Guentner"
    assert (c.agreeing, c.total) == (6, 8)
    assert not c.is_unanimous
    assert c.outliers == (("07 Fernwald.mp3", "Benoît Pioulard"), ("08 Halde.mp3", None))


def test_a_tie_refuses_to_pick():
    """The case with no honest answer. Choosing one of two equal halves would
    state something false about the album."""
    tracks = [(f"{i}.flac", "A") for i in range(4)] + [(f"{i}.flac", "B") for i in range(4, 8)]
    c = consensus(tracks)
    assert c.value is None
    assert c.total == 8


def test_no_tracks_and_no_values_are_distinguishable_from_a_tie_by_total():
    assert consensus([]) == Consensus(value=None, agreeing=0, total=0)
    empty = consensus([("01.flac", None), ("02.flac", None)])
    assert empty.value is None and empty.total == 2


# ---------- in-value emphasis ----------


def test_a_date_marks_only_the_added_precision():
    a_runs, b_runs = diff_runs("2019", "2019-03-15")
    assert _text(a_runs) == "2019" and _text(b_runs) == "2019-03-15"
    assert _changed(a_runs) == []
    assert _changed(b_runs) == ["-03-15"]


def test_separator_punctuation_is_marked_in_place():
    """The case that motivates in-value emphasis at all: two values that look
    identical at a glance and differ only in how the artists are joined."""
    a_runs, b_runs = diff_runs("Galán | Spieth | Guentner", "Galán, Spieth & Guentner")
    assert _text(a_runs) == "Galán | Spieth | Guentner"
    assert _text(b_runs) == "Galán, Spieth & Guentner"
    assert _changed(a_runs) and _changed(b_runs)
    # Only the joins, never the names.
    assert all("Spieth" not in r and "Guentner" not in r for r in _changed(a_runs))


def test_one_contiguous_change_is_marked_however_big_it_is():
    """Size alone is the wrong test for confetti — fragmentation is. A whole
    featured-artist suffix is 80% of the value and perfectly readable marked."""
    a_runs, b_runs = diff_runs("Halde (feat. Ana Quiroga)", "Halde")
    assert _changed(a_runs) == [" (feat. Ana Quiroga)"]
    assert _changed(b_runs) == []


def test_wholly_different_values_get_no_emphasis():
    """Past a third of the characters the runs stop being a signal and start
    looking like confetti — both values render plain instead."""
    assert diff_runs("Kaskade", "Rainbow Connection") == ((), ())


def test_identical_values_produce_no_runs():
    assert diff_runs("Fernwald", "Fernwald") == ((), ())


def test_runs_reconstruct_the_originals_exactly():
    """Emphasis must never alter what's displayed — a lost or duplicated
    character would misreport the user's tags."""
    for a, b in [
        ("Obreel pt. II", "Obreel, Pt. II"),
        ("2019", "2019-03-15"),
        ("Halde (feat. Ana Quiroga)", "Halde"),
    ]:
        a_runs, b_runs = diff_runs(a, b)
        if a_runs or b_runs:
            assert _text(a_runs) == a
            assert _text(b_runs) == b


# ---------- field comparison ----------


def test_matching_field_is_not_a_finding():
    f = compare_field("Album", disk=consensus([("1.flac", "Obreel")]), mb="Obreel")
    assert f.agreement is Agreement.MATCHES
    assert not f.differs
    assert f.disk_runs == () and f.mb_runs == ()


def test_absent_on_disk_reads_as_only_mb_not_as_a_conflict():
    """Label and catalogue number aren't disagreements — there's nothing of
    yours to disagree with. The row carries the MB value alone."""
    f = compare_field("Label", disk=consensus([("1.flac", None)]), mb="Dial Records")
    assert f.agreement is Agreement.ONLY_MB
    assert f.disk is None and f.mb == "Dial Records"
    assert f.differs


def test_a_field_musicbrainz_has_no_opinion_on_is_not_a_difference():
    """The comment carries the Bandcamp URL. MB has no counterpart, so
    comparing would invent a finding."""
    f = compare_field(
        "Comment",
        disk=consensus([("1.flac", "https://pioulard.bandcamp.com/album/obreel")]),
        mb=None,
    )
    assert f.agreement is Agreement.ONLY_DISK
    assert not f.differs


def test_differing_field_carries_runs_for_a_small_change():
    f = compare_field(
        "Date",
        kind=Kind.SCALAR,
        disk=consensus([("1.flac", "2019")]),
        mb="2019-03-15",
    )
    assert f.agreement is Agreement.DIFFERS
    assert f.differs
    assert _changed(f.mb_runs) == ["-03-15"]


def test_an_unreadable_file_is_neither_matching_nor_absent():
    """#112: reporting a file Harmonist couldn't open as untagged is how a
    failing disk gets an album re-tagged."""
    f = compare_field("Artist", disk=None, mb="Galán, Spieth & Guentner", unreadable=True)
    assert f.agreement is Agreement.UNREADABLE
    assert f.differs  # the user has to be told, not quietly shown an absence


def test_tracks_that_disagree_with_no_majority_say_so():
    tracks = [(f"{i}.flac", "A") for i in range(2)] + [(f"{i}.flac", "B") for i in range(2, 4)]
    f = compare_field("Artist", disk=consensus(tracks), mb="A")
    assert f.agreement is Agreement.NO_CONSENSUS
    assert f.disk is None  # nothing arbitrary was picked
    assert f.consensus is not None and f.consensus.total == 4


def test_an_untagged_field_is_absent_not_inconsistent():
    """The tracks all agree — they agree there's nothing there. Reporting that
    as "the tracks disagree" would call every field of an untagged album
    inconsistent."""
    f = compare_field(
        "Label",
        disk=consensus([("1.flac", None), ("2.flac", None), ("3.flac", None)]),
        mb="Dial Records",
    )
    assert f.agreement is Agreement.ONLY_MB


def test_a_majority_value_is_still_compared_against_musicbrainz():
    """Uneven tagging doesn't stop the comparison — it annotates it."""
    tracks = [(f"{i}.flac", "Obreel") for i in range(6)] + [("7.flac", "obreel")]
    f = compare_field("Album", disk=consensus(tracks), mb="Obreel")
    assert f.agreement is Agreement.MATCHES
    assert f.consensus is not None
    assert (f.consensus.agreeing, f.consensus.total) == (6, 7)
    assert not f.consensus.is_unanimous  # the UI has something to flag


# ---------- the album headline ----------


def test_summary_counts_only_real_findings():
    fields = (
        compare_field("Album", disk=consensus([("1.flac", "Obreel")]), mb="Obreel"),
        compare_field("Artist", disk=consensus([("1.flac", "A | B")]), mb="A, B"),
        compare_field("Label", disk=consensus([("1.flac", None)]), mb="Dial Records"),
        compare_field("Comment", disk=consensus([("1.flac", "https://x")]), mb=None),
    )
    album = AlbumComparison(fields=fields)
    # Artist and Label; NOT the matching album title, NOT the comment.
    assert len(album.differing) == 2
    assert album.summary == "2 of 4 fields differ"


def test_summary_when_everything_matches():
    fields = (compare_field("Album", disk=consensus([("1.flac", "Obreel")]), mb="Obreel"),)
    assert AlbumComparison(fields=fields).summary == "all 1 fields match"
    assert AlbumComparison().summary == "nothing to compare"
