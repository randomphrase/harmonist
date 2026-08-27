"""Tests for the disk-vs-MusicBrainz comparison model (#106).

Values throughout are real ones from a Bandcamp library, because the cases that
matter are the ones a synthetic "foo" vs "bar" never produces: separator
punctuation in an artist credit, a date MusicBrainz knows more precisely, a
featured credit MB keeps out of the title.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

from harmonist import formats
from harmonist.compare import (
    TRACK_COLUMNS,
    Agreement,
    AlbumComparison,
    ComparedTrack,
    Consensus,
    Kind,
    MBTrack,
    TrackState,
    album_fields,
    compare_field,
    consensus,
    diff_runs,
    disk_tracklist,
    tracklist,
)
from harmonist.formats.types import TagSet, TrackTags


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


def test_a_tie_is_broken_by_track_order():
    """A 4/4 split has no most-common value, so track 1 decides — a rule that
    fits in one sentence. The count beside it already tells the user this isn't
    the album's settled answer."""
    tracks = [(f"{i}.flac", "B") for i in range(4)] + [(f"{i}.flac", "A") for i in range(4, 8)]
    c = consensus(tracks)
    assert c.value == "B"  # track 1's value, not the alphabetically first
    assert (c.agreeing, c.total, c.distinct) == (4, 8, 2)
    assert not c.is_unanimous


def test_a_tie_skips_tracks_with_no_value_at_all():
    """Track 1 means the first track that actually has a value — an untagged
    opening track shouldn't decide the album has no artist."""
    tracks: list[tuple[str, str | None]] = [("01.flac", None), ("02.flac", "B"), ("03.flac", "A")]
    assert consensus(tracks).value == "B"


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


def test_evenly_split_tracks_still_produce_a_comparison():
    """Uneven tagging must not suppress the row — the comparison is made against
    track 1's value, and the consensus counts travel alongside so the UI can say
    how shaky it is."""
    tracks = [(f"{i}.flac", "A") for i in range(2)] + [(f"{i}.flac", "B") for i in range(2, 4)]
    f = compare_field("Artist", disk=consensus(tracks), mb="A")
    assert f.agreement is Agreement.MATCHES  # track 1 says "A", and so does MB
    assert f.disk == "A"
    assert f.consensus is not None
    assert (f.consensus.agreeing, f.consensus.total) == (2, 4)
    assert not f.consensus.is_unanimous  # ...but only half the album agrees


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


def _tagset(**overrides: object) -> TagSet:
    """A minimal MusicBrainz-side TagSet — the required fields, plus whatever
    the caller is actually asserting about."""
    base: dict[str, object] = {
        "mb_album_id": "rel-1",
        "album": "Obreel",
        "album_artist": "A",
        "title": "T",
        "artist": "A",
        "track_num": 1,
        "track_total": 1,
    }
    return TagSet(**{**base, **overrides})  # type: ignore[arg-type]


def test_summary_counts_only_real_findings():
    fields = (
        compare_field("Album", disk=consensus([("1.flac", "Obreel")]), mb="Obreel"),
        compare_field("Artist", disk=consensus([("1.flac", "A | B")]), mb="A, B"),
        compare_field("Label", disk=consensus([("1.flac", None)]), mb="Dial Records"),
        # No MusicBrainz counterpart at all — marked as `album_fields` marks it.
        replace(
            compare_field("Comment", disk=consensus([("1.flac", "https://x")]), mb=None),
            comparable=False,
        ),
    )
    album = AlbumComparison(fields=fields)
    # Artist and Label; NOT the matching album title, NOT the comment.
    assert len(album.differing) == 2
    # Three in the denominator, not four: MusicBrainz was never asked about the
    # comment, so counting it in a sentence about matching MusicBrainz claims a
    # check that didn't happen (#164).
    assert album.summary == "2 of 3 fields differ in MusicBrainz"


def test_summary_excludes_fields_musicbrainz_has_no_opinion_on(tmp_path):
    """End to end, through `album_fields`, where the flag is really set.

    Genre and comment are in the field table deliberately — the user should see
    a tag Harmonist is keeping for them — but MusicBrainz has no counterpart for
    either, so "All N fields match MusicBrainz" must not count them.
    """
    tags = TrackTags(
        album="Obreel", album_artist="A", genre="Ambient", comment="https://x.bandcamp.com"
    )
    mb = _tagset(album="Obreel", album_artist="A")

    fields = album_fields([("1.flac", tags)], mb)
    by_label = {f.label: f for f in fields}

    assert by_label["Genre"].comparable is False
    assert by_label["Comment"].comparable is False
    assert by_label["Album"].comparable is True
    # Nine rows shown, seven of them actually compared.
    assert len(fields) == 9
    assert len(AlbumComparison(fields=fields).comparable) == 7
    assert AlbumComparison(fields=fields).summary == "All 7 fields match MusicBrainz"


def test_unreadable_files_cannot_push_the_count_past_the_total():
    """The arithmetic that used to be possible: every field goes UNREADABLE,
    including the two that were never comparable, so a naive count could reach
    "9 of 7 differ" (#164)."""
    fields = album_fields([("1.flac", TrackTags(unreadable=True))], _tagset(album="Obreel"))
    album = AlbumComparison(fields=fields)

    n = len([f for f in album.comparable if f.differs])
    assert n <= len(album.comparable)
    assert album.summary == "7 of 7 fields differ in MusicBrainz"


# ---------- what the consensus pill says (#164) ----------


def test_pill_names_a_missing_tag_as_missing():
    """Absence and disagreement need different words because they need
    different fixes: a missing tag is filled in, a differing one reconciled."""
    c = consensus([("1.flac", "Ambient"), ("2.flac", "Ambient"), ("3.flac", None)])
    assert c.missing_count == 1
    assert c.odd_summary == "missing on 1 track"


def test_pill_names_a_differing_tag_as_differing():
    c = consensus([("1.flac", "Ambient"), ("2.flac", "Ambient"), ("3.flac", "Drone")])
    assert c.missing_count == 0
    assert c.odd_summary == "1 track differs"


def test_pill_agrees_with_itself_in_number():
    """The verb has to agree with the count, not just the noun — "1 track
    differ" is what a helper that only pluralises the noun produces."""
    two_differ = consensus([("1.flac", "A"), ("2.flac", "A"), ("3.flac", "B"), ("4.flac", "C")])
    assert two_differ.odd_summary == "2 tracks differ"

    two_missing = consensus([("1.flac", "A"), ("2.flac", "A"), ("3.flac", None), ("4.flac", None)])
    assert two_missing.odd_summary == "missing on 2 tracks"


def test_pill_says_so_when_the_outliers_are_a_mix():
    """One absent and one different is neither of the simple cases, and calling
    it either would be wrong about half of it."""
    c = consensus([("1.flac", "A"), ("2.flac", "A"), ("3.flac", "B"), ("4.flac", None)])
    assert c.odd_summary == "2 tracks differ or are missing"

    one_each = consensus([("1.flac", "A"), ("2.flac", "A"), ("3.flac", None)])
    assert one_each.odd_summary == "missing on 1 track"


# ---------- end to end: real files against a real release ----------


def _release() -> dict:
    """A MusicBrainz release with the fields the album panel shows."""
    return {
        "id": "rel-obreel",
        "title": "Obreel",
        "date": "2019-03-15",
        "barcode": "4053804203319",
        "artist-credit": [{"artist": {"id": "a1", "name": "Galán, Spieth & Guentner"}}],
        "release-group": {"id": "rg-1", "primary-type": "Album"},
        "label-info-list": [{"label": {"name": "Dial Records"}, "catalog-number": "DIAL 042"}],
        "medium-list": [
            {
                "position": "1",
                "format": "Digital Media",
                "track-list": [
                    {"id": "t1", "title": "Kaskade", "recording": {"id": "r1", "title": "Kaskade"}}
                ],
            }
        ],
    }


def test_a_tagged_album_compares_clean_against_the_release_it_was_tagged_from(tmp_path):
    """The end-to-end guarantee, and the one most worth having: tag an album
    FROM a release, compare it BACK against that release, and nothing should
    differ. Any mismatch between what the tagger writes and what the reader
    reads shows up here as a difference the user would be told to fix — against
    tags Harmonist itself just wrote."""
    from harmonist.tagger import tag_album, tagsets_for

    d = tmp_path / "Artist" / "Obreel"
    d.mkdir(parents=True)
    shutil.copy(Path(__file__).parent / "fixtures" / "sine.m4a", d / "01 Kaskade.m4a")

    release = _release()
    assert tag_album(d, release) == 1

    tracks = [(f.name, formats.read_tags(f)) for f in sorted(d.glob("*.m4a"))]
    fields = album_fields(tracks, tagsets_for(release)[0])

    differing = [f.label for f in fields if f.differs]
    assert differing == [], f"tagging then comparing reported a difference: {differing}"

    by_label = {f.label: f for f in fields}
    assert by_label["Label"].disk == "Dial Records"  # …and it really did read them
    assert by_label["Cat. no."].disk == "DIAL 042"
    assert by_label["Date"].agreement is Agreement.MATCHES


def test_an_untagged_album_shows_musicbrainz_values_as_additions(tmp_path):
    """Nothing on disk to disagree with, so every MB field is ONLY_MB — the
    'lone purple line' case, not a conflict."""
    from harmonist.tagger import tagsets_for

    d = tmp_path / "Artist" / "Untagged"
    d.mkdir(parents=True)
    shutil.copy(Path(__file__).parent / "fixtures" / "sine.m4a", d / "01 Track.m4a")

    tracks = [(f.name, formats.read_tags(f)) for f in sorted(d.glob("*.m4a"))]
    by_label = {f.label: f for f in album_fields(tracks, tagsets_for(_release())[0])}

    assert by_label["Label"].agreement is Agreement.ONLY_MB
    assert by_label["Label"].mb == "Dial Records"
    assert by_label["Label"].disk is None


def test_an_unreadable_track_does_not_report_its_tags_as_missing(tmp_path):
    """#112 reaching the comparison: a file Harmonist couldn't open must not
    vote 'absent' and drag every field to ONLY_MB, which would tell the user
    their tags are gone when the truth is Harmonist couldn't look."""
    from harmonist.tagger import tagsets_for

    d = tmp_path / "Artist" / "Broken"
    d.mkdir(parents=True)
    shutil.copy(Path(__file__).parent / "fixtures" / "sine.m4a", d / "01 Track.m4a")
    (d / "01 Track.m4a").write_bytes(b"not audio at all")

    tracks = [(f.name, formats.read_tags(f)) for f in sorted(d.glob("*.m4a"))]
    fields = album_fields(tracks, tagsets_for(_release())[0])

    assert all(f.agreement is Agreement.UNREADABLE for f in fields)
    assert not any(f.agreement is Agreement.ONLY_MB for f in fields)


def test_summary_when_everything_matches():
    fields = (compare_field("Album", disk=consensus([("1.flac", "Obreel")]), mb="Obreel"),)
    assert AlbumComparison(fields=fields).summary == "All 1 fields match MusicBrainz"
    assert AlbumComparison().summary == "Nothing to compare against MusicBrainz"


# ---------- the tracklist (#135) ----------


def _mb_track(num: int, title: str, *, artist="Kavinsky", length=180_000, disc=1) -> MBTrack:
    """One MusicBrainz track as the comparison sees it — what tagging would
    write, plus the length, which is not a tag."""
    return MBTrack(
        tags=TagSet(
            mb_album_id="rel-1",
            album="OutRun",
            album_artist="Kavinsky",
            title=title,
            artist=artist,
            track_num=num,
            track_total=4,
            disc_num=disc,
        ),
        length_ms=length,
    )


def _file(num: int | None, title: str, *, artist="Kavinsky", length=180_000, disc=None):
    """One file on disk, named after its number the way a tagger would."""
    name = f"{num:02d} {title}.flac" if num is not None else f"{title}.flac"
    return name, TrackTags(
        title=title, artist=artist, track_num=num, disc_num=disc, duration_ms=length
    )


def _labels(track: ComparedTrack) -> list[str]:
    return [f.label for f in track.fields]


def test_every_row_carries_the_columns_in_order():
    """The template renders one column per field, positionally, and takes its
    headings from TRACK_COLUMNS — so a field reordered here without the headings
    would silently put values under the wrong column."""
    tl = tracklist([_file(1, "Nightcall")], [_mb_track(1, "Nightcall")])
    assert _labels(tl.tracks[0]) == list(TRACK_COLUMNS)


def test_a_faithfully_tagged_album_shows_no_differences():
    tl = tracklist(
        [_file(1, "Nightcall"), _file(2, "Odd Look")],
        [_mb_track(1, "Nightcall"), _mb_track(2, "Odd Look")],
    )
    assert [t.state for t in tl.tracks] == [TrackState.PRESENT] * 2
    assert tl.differing == ()
    assert tl.summary == "All 2 tracks match MusicBrainz"


def test_a_differing_title_is_the_only_thing_flagged():
    """The row reports the title and nothing else — the register is 'here is
    what differs', not 'this track is wrong'."""
    tl = tracklist([_file(1, "Odd Look")], [_mb_track(1, "Odd Look (feat. Kaas)")])
    (row,) = tl.tracks
    assert row.state is TrackState.PRESENT
    assert row.differs and row.shows_mb
    flagged = {f.label: f for f in row.fields if f.differs}
    assert list(flagged) == ["Title"]
    assert flagged["Title"].mb == "Odd Look (feat. Kaas)"
    assert tl.summary == "1 of 1 tracks differs from MusicBrainz"


# ---------- pairing files to MusicBrainz tracks ----------


def test_a_missing_track_does_not_shift_every_row_after_it():
    """The reason pairing is by number rather than position. With files 1, 2 and
    4 against a four-track release, positional pairing would compare file 4
    against track 3 and report the last two tracks as differing — two false
    findings from one absent file."""
    tl = tracklist(
        [_file(1, "Nightcall"), _file(2, "Odd Look"), _file(4, "Blizzard")],
        [
            _mb_track(1, "Nightcall"),
            _mb_track(2, "Odd Look"),
            _mb_track(3, "Protovision"),
            _mb_track(4, "Blizzard"),
        ],
    )
    assert [t.state for t in tl.tracks] == [
        TrackState.PRESENT,
        TrackState.PRESENT,
        TrackState.MISSING,
        TrackState.PRESENT,
    ]
    # Exactly one finding: the track that genuinely isn't there.
    assert len(tl.differing) == 1
    assert tl.summary == "1 of 4 tracks differs from MusicBrainz · 1 not on disk"


def test_a_missing_track_shows_what_musicbrainz_says_is_absent():
    tl = tracklist([_file(1, "Nightcall")], [_mb_track(1, "Nightcall"), _mb_track(2, "Odd Look")])
    missing = tl.tracks[1]
    assert missing.state is TrackState.MISSING
    assert missing.file_name is None
    assert missing.shows_mb  # ...so the row can say WHICH track is gone
    assert {f.label: f.mb for f in missing.fields}["Title"] == "Odd Look"
    assert all(f.disk is None for f in missing.fields)


def test_files_with_no_track_numbers_fall_back_to_file_order():
    """An album that was never numbered behaves exactly as positional pairing
    always did — the heuristic degrades, it doesn't refuse."""
    tl = tracklist(
        [_file(None, "Nightcall"), _file(None, "Odd Look")],
        [_mb_track(1, "Nightcall"), _mb_track(2, "Odd Look")],
    )
    assert [t.state for t in tl.tracks] == [TrackState.PRESENT] * 2
    # The number itself is a real difference: MB has one, the file doesn't.
    numbers = [f for t in tl.tracks for f in t.fields if f.label == "#"]
    assert all(f.agreement is Agreement.ONLY_MB for f in numbers)


def test_duplicate_track_numbers_are_not_trusted():
    """Two files both claiming track 1 is an ambiguity, not an assignment. Both
    fall back to file order rather than one of them winning the slot."""
    tl = tracklist(
        [_file(1, "Nightcall"), ("02 Odd Look.flac", TrackTags(title="Odd Look", track_num=1))],
        [_mb_track(1, "Nightcall"), _mb_track(2, "Odd Look")],
    )
    assert [t.file_name for t in tl.tracks] == ["01 Nightcall.flac", "02 Odd Look.flac"]
    assert [t.state for t in tl.tracks] == [TrackState.PRESENT] * 2


def test_track_four_of_disc_two_is_not_track_four_of_disc_one():
    """The disc is part of the key. Without it both discs' track 4 collide and
    the comparison pairs the wrong halves of the album against each other."""
    mb = [
        _mb_track(1, "Nightcall", disc=1),
        _mb_track(1, "Protovision", disc=2),
    ]
    tl = tracklist(
        [_file(1, "Protovision", disc=2), _file(1, "Nightcall", disc=1)],
        mb,
    )
    assert [t.file_name for t in tl.tracks] == ["01 Nightcall.flac", "01 Protovision.flac"]
    assert tl.differing == ()
    # ...and a multi-disc release numbers its rows "disc-track", because "1"
    # alone doesn't identify a track on it.
    assert [f.disk for f in tl.tracks[1].fields if f.label == "#"] == ["2-1"]


def test_an_extra_file_is_stated_not_warned_about():
    """A bonus track MusicBrainz doesn't carry. It gets a row of its own with no
    MusicBrainz line to draw against it."""
    tl = tracklist([_file(1, "Nightcall"), _file(2, "Untitled Bonus")], [_mb_track(1, "Nightcall")])
    extra = tl.tracks[1]
    assert extra.state is TrackState.EXTRA
    assert extra.differs  # the user should see it
    assert not extra.shows_mb  # ...but there is nothing to show it against
    assert tl.summary == "1 of 2 tracks differs from MusicBrainz · 1 not in MusicBrainz"


# ---------- lengths ----------


def test_a_length_within_tolerance_is_not_a_difference():
    """Same constant the matcher uses. A page that flagged a 2-second gap would
    contradict the verdict Harmonist already acted on for this very release."""
    tl = tracklist(
        [_file(1, "Nightcall", length=182_000)], [_mb_track(1, "Nightcall", length=180_000)]
    )
    (length,) = [f for f in tl.tracks[0].fields if f.label == "Length"]
    assert length.agreement is Agreement.MATCHES
    assert length.disk == "3:02"  # the file's own length, which is the audio you have
    assert not tl.differing


def test_a_length_beyond_tolerance_is():
    tl = tracklist(
        [_file(1, "Nightcall", length=240_000)], [_mb_track(1, "Nightcall", length=180_000)]
    )
    (length,) = [f for f in tl.tracks[0].fields if f.label == "Length"]
    assert length.agreement is Agreement.DIFFERS
    assert (length.disk, length.mb) == ("4:00", "3:00")


def test_a_length_musicbrainz_does_not_know_is_not_a_difference_either():
    """MB carries no length for plenty of digital releases. Absent on their side
    is ONLY_DISK — nothing of theirs to disagree with — not a finding."""
    tl = tracklist([_file(1, "Nightcall")], [_mb_track(1, "Nightcall", length=None)])
    (length,) = [f for f in tl.tracks[0].fields if f.label == "Length"]
    assert length.agreement is Agreement.ONLY_DISK
    assert not tl.differing


# ---------- unreadable files (#112, #126) ----------


def test_an_unreadable_file_is_not_a_missing_one():
    """Three distinct answers, three distinct remedies: the file is there and
    won't open. Reporting it as untagged is #112; reporting it as absent would
    send the user looking for a track they already have."""
    tl = tracklist(
        [("01 Nightcall.flac", TrackTags(unreadable=True))],
        [_mb_track(1, "Nightcall")],
    )
    (row,) = tl.tracks
    assert row.state is TrackState.UNREADABLE
    assert row.file_name == "01 Nightcall.flac"  # "which one?" is the next question
    assert all(f.agreement is Agreement.UNREADABLE for f in row.fields)
    assert all(f.disk is None for f in row.fields)
    assert row.shows_mb  # MB still says what the track should be
    assert tl.summary == "1 of 1 tracks differs from MusicBrainz · 1 unreadable"


def test_an_unreadable_file_takes_the_slot_left_free_by_the_numbered_ones():
    """It carries no tags at all, so it can't be placed by number — but the
    numbered files around it place themselves, and the gap they leave is where
    it belongs."""
    tl = tracklist(
        [
            _file(1, "Nightcall"),
            ("02 Odd Look.flac", TrackTags(unreadable=True)),
            _file(3, "Protovision"),
        ],
        [_mb_track(1, "Nightcall"), _mb_track(2, "Odd Look"), _mb_track(3, "Protovision")],
    )
    assert [t.state for t in tl.tracks] == [
        TrackState.PRESENT,
        TrackState.UNREADABLE,
        TrackState.PRESENT,
    ]
    assert tl.tracks[1].file_name == "02 Odd Look.flac"


def test_an_album_with_no_release_to_compare_against_says_so():
    assert tracklist([], []).summary == "Nothing to compare against MusicBrainz"


# ---------- the disk-only view: MusicBrainz has deleted the release (#228) ----------


def test_a_disk_only_tracklist_shows_the_tracks_without_calling_them_extra():
    """The tracks never depended on MusicBrainz, so they still render — but
    nothing here is a finding: MusicBrainz was never asked. `tracklist(t, [])`
    reaches a similar shape by calling every row EXTRA ("not in MusicBrainz"),
    which is a claim about the track rather than about the release."""
    tl = disk_tracklist([_file(1, "Nightcall"), _file(2, "Odd Look")])

    assert [t.state for t in tl.tracks] == [TrackState.PRESENT, TrackState.PRESENT]
    assert [t.fields[1].disk for t in tl.tracks] == ["Nightcall", "Odd Look"]
    assert not tl.mb_available
    assert not any(t.shows_mb for t in tl.tracks), "no MusicBrainz line to draw"


def test_a_disk_only_tracklist_says_it_compared_nothing():
    """The header note is the one place the absence is stated. Left to the
    default it would have said "All 2 tracks match MusicBrainz"."""
    assert disk_tracklist([_file(1, "Nightcall"), _file(2, "Odd Look")]).summary == (
        "No comparison — showing your 2 tracks"
    )
    assert disk_tracklist([_file(1, "Nightcall")]).summary == (
        "No comparison — showing your 1 track"
    )


def test_a_disk_only_tracklist_keeps_the_unreadable_state():
    """A file that won't open is still a file that won't open — that answer
    doesn't come from MusicBrainz, so losing it here would report a dead file as
    a track shown normally."""
    tl = disk_tracklist([("01 Nightcall.flac", TrackTags(unreadable=True))])
    (row,) = tl.tracks
    assert row.state is TrackState.UNREADABLE
    assert row.file_name == "01 Nightcall.flac"


def test_a_disk_only_tracklist_groups_by_the_files_own_discs():
    tl = disk_tracklist([_file(1, "Nightcall", disc=1), _file(1, "Protovision", disc=2)])
    assert [g.medium.position for g in tl.discs] == [1, 2]


def test_a_disk_only_album_panel_says_it_compared_nothing():
    """`album_fields(tracks, None)` already leaves every field ONLY_DISK, which
    is deliberately not a finding — so without `mb_available` the summary read
    "All 7 fields match MusicBrainz" for a release MusicBrainz has deleted."""
    fields = album_fields([_file(1, "Nightcall")], None)
    assert AlbumComparison(fields=fields).summary.startswith("All ")
    assert AlbumComparison(fields=fields, mb_available=False).summary == (
        "No comparison — showing your files' tags"
    )


# ---------- Picard's disambiguated album title (#283) ----------


def _album_row(disk_album: str, *, alias: str | None = None, **track_overrides):
    tags = TrackTags(album=disk_album, **track_overrides)
    fields = album_fields([("1.flac", tags)], _tagset(album="Obreel"), album_title_alias=alias)
    return {f.label: f for f in fields}


def test_the_disambiguated_album_title_reads_as_a_match():
    """Picard appends the release disambiguation to the album title when told to,
    so `Obreel (expanded edition)` and `Obreel` are the same album by the user's
    own setting — not a difference to report on every page view forever (#283).

    The row still reports what is really on disk. Normalising the displayed value
    to MusicBrainz's spelling would make the panel claim the files say something
    they don't, which is the one thing this table exists to be trusted about.
    """
    row = _album_row("Obreel (expanded edition)", alias="Obreel (expanded edition)")["Album"]

    assert row.agreement is Agreement.MATCHES
    assert row.disk == "Obreel (expanded edition)"


def test_the_same_title_differs_when_the_release_has_no_disambiguation():
    """The other half, and what makes the test above mean something: with no
    disambiguation there is no second spelling to accept, so the identical disk
    value is a genuine difference."""
    row = _album_row("Obreel (expanded edition)")["Album"]

    assert row.agreement is Agreement.DIFFERS


def test_only_the_disambiguation_is_accepted_not_any_parenthetical():
    """One exact string, never a pattern. `models.titles_match` would accept this
    on the strength of the words alone, and it is right to where it is used —
    inside an artist-scoped, uniqueness-guarded purchase match. Here the release
    states its disambiguation exactly, so accepting more would be guessing an
    identity that was available for free (review-gate item 2).
    """
    row = _album_row("Obreel (deluxe edition)", alias="Obreel (expanded edition)")["Album"]

    assert row.agreement is Agreement.DIFFERS


def test_the_alias_applies_to_the_album_row_alone():
    """Scoped to the one field with a second legitimate spelling. Handed to every
    row it would silently excuse a real difference anywhere the string happened
    to collide — an album artist genuinely renamed to the album's own title is
    daft, and is exactly the kind of thing a loose special case lets through.
    """
    rows = _album_row(
        "Obreel (expanded edition)",
        alias="Obreel (expanded edition)",
        album_artist="Obreel (expanded edition)",
    )

    assert rows["Album"].agreement is Agreement.MATCHES
    assert rows["Album artist"].agreement is Agreement.DIFFERS
