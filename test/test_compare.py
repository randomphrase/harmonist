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
    MAX_EARNED_COLUMNS,
    PANEL_FIELDS,
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
from harmonist.formats.owned import ALBUM_FIELDS, LABELS, Owned
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
    # Every album-scoped tag Harmonist writes is compared bar one, and the two it
    # only displays are not. Stated against `owned` rather than as literals: the
    # gap between a hand-written count here and the real field set is what let
    # this panel omit twenty-one fields (#295), and a test asserting "19" would
    # have to be edited by the same person who forgot to add the field.
    #
    # The exception is spelled out rather than imported, so a SECOND row dropped
    # from the panel has to be argued for here. `mb_album_id` is the release the
    # comparison is fetched by, and the header already shows and links it, so the
    # row matched on every album but a merge (#298).
    from harmonist.formats.owned import ALBUM_FIELDS, LABELS, Owned

    compared = [f for f in ALBUM_FIELDS if f is not Owned.MB_ALBUM_ID]
    assert len(fields) == len(compared) + 2
    comparable = AlbumComparison(fields=fields).comparable
    assert len(comparable) == len(compared)
    assert {f.label for f in comparable} == {LABELS[f] for f in compared}


def test_unreadable_files_cannot_push_the_count_past_the_total():
    """The arithmetic that used to be possible: every field goes UNREADABLE,
    including the two that were never comparable, so a naive count could reach
    "9 of 7 differ" (#164)."""
    fields = album_fields([("1.flac", TrackTags(unreadable=True))], _tagset(album="Obreel"))
    album = AlbumComparison(fields=fields)

    from harmonist.formats.owned import ALBUM_FIELDS, Owned

    # Less the one row the panel doesn't carry — see the note in
    # `test_summary_excludes_fields_musicbrainz_has_no_opinion_on` (#298).
    total = len([f for f in ALBUM_FIELDS if f is not Owned.MB_ALBUM_ID])
    n = len([f for f in album.comparable if f.differs])
    assert n <= len(album.comparable)
    assert album.summary == f"{total} of {total} fields differ in MusicBrainz"


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


#: What the album's release says its media hold, on both sides of the fixture —
#: named rather than written twice, because a file and a release disagreeing
#: about the track total is a difference the tracklist now reports (#309), and
#: two literals drifting apart would make every fixture album report one.
_TRACK_TOTAL = 4


def _mb_track(
    num: int, title: str, *, artist="Kavinsky", length=180_000, disc=1, total=_TRACK_TOTAL, **tags
) -> MBTrack:
    """One MusicBrainz track as the comparison sees it — what tagging would
    write, plus the length, which is not a tag.

    `**tags` sets any other TagSet field, so a test can name the one thing
    MusicBrainz says that the file does not.
    """
    return MBTrack(
        tags=TagSet(
            mb_album_id="rel-1",
            album="OutRun",
            album_artist="Kavinsky",
            title=title,
            artist=artist,
            track_num=num,
            track_total=total,
            disc_num=disc,
            **tags,
        ),
        length_ms=length,
    )


def _file(num: int | None, title: str, *, artist="Kavinsky", length=180_000, disc=1, **owned):
    """One file on disk, named after its number the way a tagger would.

    Carries the `owned` snapshot a real `read_tags` takes off the handle, not
    just the four named attributes. Without it every per-track tag MusicBrainz
    has and the fixture doesn't reads as a difference — and since #309 the
    tracklist's COLUMNS are derived from exactly those differences, so a thin
    fixture doesn't merely under-test, it changes the shape of the table under
    every assertion about it. (A fixture narrower than the code is how
    KNOWN_GAPS asserted for months that Harmonist doesn't write `DISCSUBTITLE`
    while the tagger was writing it.)

    Defaults to a file `_mb_track` agrees with completely, so a test makes ONE
    thing differ and names it in the call: `_file(6, "…", artist_sort="…")`.
    """
    name = f"{num:02d} {title}.flac" if num is not None else f"{title}.flac"
    snapshot = {
        "title": title,
        "artist": artist,
        "album": "OutRun",
        "album_artist": "Kavinsky",
        "track_num": num,
        "track_total": _TRACK_TOTAL,
        "disc_num": disc,
    }
    snapshot.update(owned)
    return name, TrackTags(
        title=title,
        artist=artist,
        track_num=num,
        disc_num=disc,
        duration_ms=length,
        owned=snapshot,
    )


def _labels(track: ComparedTrack) -> list[str]:
    return [f.label for f in track.fields]


def test_every_row_carries_the_columns_in_order():
    """The template renders one <td> per field, positionally, under headings it
    takes from `columns` — so a row whose fields disagree with them in order or
    in LENGTH puts every value after the divergence under the wrong heading.

    Asserted over every row of an album that has one of each state, because the
    rows are built by four different branches and only the present one is
    exercised by an album where nothing is wrong.
    """
    short = tracklist(
        [_file(1, "Nightcall"), ("dead.flac", TrackTags(unreadable=True))],
        [_mb_track(1, "Nightcall"), _mb_track(2, "Odd Look"), _mb_track(3, "Rampage")],
    )
    over = tracklist(
        [_file(1, "Nightcall"), _file(2, "Testarossa Autodrive")],
        [_mb_track(1, "Nightcall")],
    )
    assert {t.state for t in short.tracks} == {
        TrackState.PRESENT,
        TrackState.UNREADABLE,
        TrackState.MISSING,
    }
    assert TrackState.EXTRA in {t.state for t in over.tracks}

    for tl in (short, over):
        headings = [c.label for c in tl.columns]
        assert headings[0] == "#" and headings[-1] == "Length"
        assert all(_labels(t) == headings for t in tl.tracks)


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


def test_a_field_outside_the_old_nine_is_compared(tmp_path):
    """The bug #295 is about, in the shape it was found in.

    *Selected Ambient Works, Volume II* was listed under Update available while
    its page reported every field matching: MusicBrainz had an `original_date`
    the files did not carry, and `original_date` was not one of the nine fields
    this panel compared. The panel was not being terse — its "N of M fields
    differ" line was measuring a denominator that had nothing to do with what a
    re-tag would write.

    Goes through `formats.read_tags` rather than a hand-built `TrackTags`,
    because the fix depends on the `owned` snapshot being taken off the handle
    that read the file: a fixture that filled `owned` by hand would pass with
    the backends still not populating it.
    """
    d = tmp_path / "Artist" / "Album"
    d.mkdir(parents=True)
    dst = d / "01 Track.m4a"
    shutil.copy(Path(__file__).parent / "fixtures" / "sine.m4a", dst)
    formats.write_tags(dst, _tagset(album="Obreel", original_date="2019-03-15"), None)

    tags = formats.read_tags(dst)

    # MusicBrainz has an original date the files DO carry — so the row must read
    # MATCHES. Asserting a difference instead would prove nothing: an absent disk
    # side is what a broken snapshot produces too, and the test would pass with
    # the backends not populating `owned` at all.
    matching = album_fields(
        [("01 Track.m4a", tags)], _tagset(album="Obreel", original_date="2019-03-15")
    )
    by_label = {f.label: f for f in matching}
    assert by_label["Original date"].agreement is Agreement.MATCHES
    assert by_label["Original date"].disk == "2019-03-15"

    # And when it genuinely differs, the row says so — the Aphex case.
    differing = album_fields(
        [("01 Track.m4a", tags)], _tagset(album="Obreel", original_date="1994-03-07")
    )
    row = {f.label: f for f in differing}["Original date"]
    assert row.agreement is Agreement.DIFFERS
    assert (row.disk, row.mb) == ("2019-03-15", "1994-03-07")


def test_panel_fields_names_exactly_the_album_tags_the_panel_compares():
    """The album half of what scopes the re-tag box (#297).

    As a whole set, not a sample: `PANEL_FIELDS` is derived, so a field added to
    `Owned` lands OUTSIDE it by default and starts appearing in the box. That is
    the right default, and it should still be a decision someone made rather than
    one that happened to them.
    """
    assert {f.value for f in Owned} - PANEL_FIELDS == {
        # Not a per-track tag: the panel dropped this row because the fetch is
        # made BY this id and the header already links it, so the box is where a
        # merge that moved it now surfaces (#298).
        "mb_album_id",
        # Every per-track tag. These are the tracklist's to place, and where it
        # places them depends on the album — see the columns tests below.
        "title",
        "artist",
        "artist_sort",
        "artists",
        "track_num",
        "track_total",
        "disc_num",
        "disc_subtitle",
        "media",
        "mb_track_id",
        "mb_release_track_id",
        "mb_artist_ids",
        "isrcs",
    }


# ---------- which columns a tracklist earns (#309) ----------


def _headings(tl) -> list[str]:
    return [c.label for c in tl.columns]


def test_a_column_every_track_agrees_on_is_named_below_instead_of_shown():
    """The change #309 exists for, from the quiet end.

    A single-artist album's `Artist` column is its album artist printed once per
    row: it says nothing, and it was costing a quarter of a table whose width is
    the whole problem. It is dropped — but NAMED, because a column that simply
    vanished is indistinguishable from a tag Harmonist never looked at (#112).
    """
    tl = tracklist(
        [_file(1, "Nightcall"), _file(2, "Odd Look")],
        [_mb_track(1, "Nightcall"), _mb_track(2, "Odd Look")],
    )

    assert _headings(tl) == ["#", "Title", "Length"]
    collapsed = {c.label: c.value for c in tl.collapsed}
    assert collapsed["Artist"] == "Kavinsky"
    # Its one value is still on the page, and so is the fact that it was checked.
    assert "Artist" in tl.collapsed_summary
    assert tl.collapsed_summary.endswith("are the same on every track and match MusicBrainz")


def test_a_field_differing_on_one_track_earns_a_column_there():
    """#309's own example: one field, one track, and the box could only ever say
    "1 of 7 tracks" — correct, and not enough to act on."""
    # The shape of the real one: a sort name the pre-#183 tagger wrote without
    # its join phrase, on the one track with two credited artists.
    tl = tracklist(
        [
            _file(1, "Nightcall", artist_sort="Kavinsky"),
            _file(2, "Odd Look", artist_sort="KavinskySebastian"),
        ],
        [
            _mb_track(1, "Nightcall", artist_sort="Kavinsky"),
            _mb_track(2, "Odd Look", artist_sort="Kavinsky & Sebastian"),
        ],
    )

    assert _headings(tl) == ["#", "Title", "Artist sort", "Length"]
    assert [c.label for c in tl.collapsed] and "Artist sort" not in tl.collapsed_summary
    # And it is on the row it belongs to, which is the entire point.
    sort_cells = [next(f for f in t.fields if f.label == "Artist sort") for t in tl.tracks]
    assert [f.differs for f in sort_cells] == [False, True]
    assert (sort_cells[1].disk, sort_cells[1].mb) == ("KavinskySebastian", "Kavinsky & Sebastian")


def test_tracks_disagreeing_with_each_other_earn_a_column_without_musicbrainz():
    """Rule 2, which is what an album tagged unevenly over decades trips — and it
    fires with no MusicBrainz difference at all, which rule 1 alone cannot see."""
    tl = tracklist(
        [_file(1, "Nightcall", artist="Kavinsky"), _file(2, "Odd Look", artist="Kavinsky ")],
        [_mb_track(1, "Nightcall"), _mb_track(2, "Odd Look", artist="Kavinsky ")],
    )
    assert "Artist" in _headings(tl)


def test_a_featured_credit_earns_the_artist_column_back():
    """Rule 3. Every track matches MusicBrainz and the tracks all differ from
    each other only on the one that carries a guest — but track 6 is credited to
    two artists where the album is credited to one, and that IS the finding."""
    guest = "Kavinsky feat. Lovefoxxx"
    tl = tracklist(
        [_file(1, "Nightcall"), _file(2, "Odd Look", artist=guest)],
        [_mb_track(1, "Nightcall"), _mb_track(2, "Odd Look", artist=guest)],
    )

    assert "Artist" in _headings(tl)
    assert not any(t.differs for t in tl.tracks)  # nothing here differs from MB


def test_a_change_that_reads_the_same_on_every_track_is_left_to_the_box():
    """Rule 1's second half, isolated to the one field it turns on.

    ISRC has no album-level counterpart and the files carry none of it, so rules
    2 and 3 are both silent and only rule 1 can decide. When MusicBrainz has one
    ISRC for the whole album, every track reads "— → that", the box's single line
    is the entire fact, and a column would spend one of three slots printing a
    position nobody asked for. When MusicBrainz has a different one per track,
    "which track" has an answer and the column is the only thing that gives it.
    """

    def album(*isrcs: str):
        return tracklist(
            [_file(1, "Nightcall"), _file(2, "Odd Look")],
            [
                _mb_track(1, "Nightcall", isrcs=[isrcs[0]]),
                _mb_track(2, "Odd Look", isrcs=[isrcs[1]]),
            ],
        )

    uniform = album("FRZ109800001", "FRZ109800001")
    assert "ISRC" not in _headings(uniform)
    assert "ISRC" not in {c.label for c in uniform.collapsed}, "it differs — that is not agreement"
    assert "isrcs" not in uniform.shown_fields, "so the box states it, once"

    assert "ISRC" in _headings(album("FRZ109800001", "FRZ109800002"))


def test_identifier_columns_are_off_the_cap_and_named_by_their_control():
    """#319. An MBID and an ISRC are correct to keep and correct to link, and not
    what anyone opens this page to look at — so they start hidden, and a hidden
    column has no business spending one of the three slots the readable tags are
    competing for.

    They are still COLUMNS, and `shown_fields` still claims them: the control
    that reveals them is named and states what is behind it, which is the same
    standing the collapsed set has. A hidden column and a column nobody checked
    must not read the same (#112).
    """
    tl = tracklist(
        [_file(1, "Nightcall"), _file(2, "Odd Look")],
        [
            _mb_track(
                i,
                t,
                artist_sort=f"Kavinsky {i}",
                isrcs=[f"FRZ10980000{i}"],
                mb_track_id=f"rec-{i}",
                mb_release_track_id=f"trk-{i}",
            )
            for i, t in enumerate(["Nightcall", "Odd Look"], 1)
        ],
    )

    assert [c.label for c in tl.identifier_columns] == ["ISRC", "Recording", "Release track"]
    # Three identifiers AND the readable one, on a cap of three: the identifiers
    # did not crowd out `Artist sort`, which is the whole point.
    assert "Artist sort" in _headings(tl)
    assert tl.shown_fields >= {"isrcs", "mb_track_id", "mb_release_track_id"}
    assert tl.identifier_summary == "ISRC, Recording and Release track differ here."


def test_a_row_whose_only_difference_is_hidden_hides_its_musicbrainz_line_too():
    """`shows_mb` exists to stop a row being given "an empty purple row carrying
    only a hexagon — a difference marked against nothing". Hiding the identifier
    CELLS while leaving the line recreated exactly that, one #319 later: three
    tracks, each with a lone hexagon under it and nothing beside it.

    A display state, not a finding — the line is still worth drawing, and is
    drawn the moment the identifiers are revealed.
    """
    hidden = tracklist(
        [_file(1, "Nightcall"), _file(2, "Odd Look")],
        [
            _mb_track(i, t, mb_track_id=f"rec-{i}")
            for i, t in enumerate(["Nightcall", "Odd Look"], 1)
        ],
    )
    assert all(t.shows_mb for t in hidden.tracks), "there IS a line, once you ask"
    assert all(t.mb_only_identifiers for t in hidden.tracks)

    # A readable difference on the same row and the line stays: it has something
    # to show without the identifiers being revealed.
    mixed = tracklist(
        [_file(1, "Nightcall"), _file(2, "Odd Look")],
        [
            _mb_track(1, "Nightcall", mb_track_id="rec-1"),
            _mb_track(2, "Odd Look (radio edit)", mb_track_id="rec-2"),
        ],
    )
    assert [t.mb_only_identifiers for t in mixed.tracks] == [True, False]


def test_the_table_stops_at_the_cap_and_the_rest_falls_to_the_box():
    """The second limit on rule 1, for the album where its first limit does not
    bite: an inconsistently tagged one, where the readable per-track tags part
    company track by track and would all arrive at once.

    A multi-disc release, because since #319 it is the only shape that can reach
    the cap. Within one medium the readable per-track tags are title (pinned),
    artist, artist sort and artists — three earnable, which is the cap exactly.
    The medium-derived three are what push it over.

    What overflows is deliberately NOT collapsed: the collapsed set claims the
    field is the same on every track and matches MusicBrainz, which of a field
    that overflowed *because* it differs would be false. It goes to the re-tag
    box, which is the one surface left that can state it — so `shown_fields`
    must not claim it.
    """
    tl = tracklist(
        [
            _file(1, "Nightcall", disc=1, track_total=None),
            _file(1, "Rampage", disc=2, track_total=None),
        ],
        [
            _mb_track(
                1,
                t,
                disc=d,
                total=d,
                artist=f"Kavinsky {d}",
                artist_sort=f"Kavinsky, {d}",
                artists=[f"Kavinsky {d}"],
                disc_subtitle=f"Side {d}",
                media="CD" if d == 1 else "DVD",
            )
            for d, t in ((1, "Nightcall"), (2, "Rampage"))
        ],
    )

    earned = [h for h in _headings(tl) if h not in ("#", "Title", "Length")]
    assert earned == ["Artist", "Artist sort", "Artists"]  # priority order
    assert len(earned) == MAX_EARNED_COLUMNS
    # The medium-derived three overflowed. Nothing collapsed them, and the table
    # does not claim them — so the box picks them up.
    assert not {c.label for c in tl.collapsed} & {"Disc subtitle", "Media", "Track total"}
    assert not tl.shown_fields & {"disc_subtitle", "media", "track_total"}


def test_the_artist_column_accounts_for_artists_rather_than_repeating_it():
    """#319. `artists` is the same credit unjoined, and since #309 the `Artist`
    column renders the artists it names as links — so a column beside it saying
    "Ben Lukas Boysen; Sebastian Plano" is one fact twice.

    Absorbed, not collapsed and not boxed: it is not missing from the page, it is
    there and spelled better. The invariant survives — `shown_fields` claims it,
    so the box cannot list it too.
    """
    # The compilation shape: every track a different artist, so `artist` and
    # `artists` BOTH earn by rule 2, and both are correct on disk.
    tl = tracklist(
        [
            _file(1, "Nightcall", artist="Bing & Ruth", artists=["Bing & Ruth"]),
            _file(2, "Odd Look", artist="Nils Frahm", artists=["Nils Frahm"]),
        ],
        [
            _mb_track(1, "Nightcall", artist="Bing & Ruth", artists=["Bing & Ruth"]),
            _mb_track(2, "Odd Look", artist="Nils Frahm", artists=["Nils Frahm"]),
        ],
    )

    assert "Artist" in _headings(tl), "the tracks disagree, so it earns"
    assert "Artists" not in _headings(tl)
    assert "Artists" not in {c.label for c in tl.collapsed}
    assert {"artist", "artists"} <= tl.shown_fields, "accounted for, so never in the box"


def test_artists_keeps_its_own_column_when_it_has_its_own_difference():
    """The guard on that. `Artist` can only stand in for `artists` while there is
    nothing of its own to report — a column cannot represent a change it is not
    showing, and swallowing one would be the "never none" half of #309's
    invariant broken quietly."""
    tl = tracklist(
        [_file(1, "Nightcall", artist="Bing & Ruth"), _file(2, "Odd Look", artist="Nils Frahm")],
        [
            _mb_track(1, "Nightcall", artist="Bing & Ruth", artists=["Bing", "Ruth"]),
            _mb_track(2, "Odd Look", artist="Nils Frahm", artists=["Nils Frahm"]),
        ],
    )

    assert "Artists" in _headings(tl)


def test_a_tag_musicbrainz_does_not_have_is_not_collapsed_as_agreement():
    """The third destination, and the one that is easy to lose.

    A per-track tag the files carry and MusicBrainz does not is deliberately NOT
    a difference — ONLY_DISK never reads as a finding, or the recovered Bandcamp
    URL becomes one — so it earns no column. But it is a CHANGE: a re-tag removes
    it. Collapsing it would file it under "the same on every track and matches
    MusicBrainz", which is false twice, and would then also leave it in the box —
    the same field in two places, saying opposite things.
    """
    tl = tracklist(
        [
            _file(1, "Nightcall", artist_sort="Kavinsky"),
            _file(2, "Odd Look", artist_sort="Kavinsky"),
        ],
        [_mb_track(1, "Nightcall"), _mb_track(2, "Odd Look")],  # MusicBrainz has no sort name
    )

    assert "Artist sort" not in _headings(tl), "not a difference, so not a column"
    assert "Artist sort" not in {c.label for c in tl.collapsed}, "and not agreement either"
    assert "artist_sort" not in tl.shown_fields, "so the box is free to state the removal"


def test_the_disk_only_view_collapses_on_agreement_between_the_tracks_alone():
    """MusicBrainz has deleted the release (#228), so every field is ONLY_DISK
    and `_matches_everywhere` has nothing to check. The tracks still agree with
    each other, which is all that was looked at — and all the summary claims."""
    tl = disk_tracklist([_file(1, "Nightcall"), _file(2, "Odd Look")])

    assert "Artist" in {c.label for c in tl.collapsed}
    assert tl.collapsed_summary.endswith("are the same on every track")
    assert "MusicBrainz" not in tl.collapsed_summary


def test_the_collapsed_summary_names_the_fields_rather_than_counting_them():
    """ "5 fields hidden" is a fact about the table; naming them is a finding
    about the album, and it is what keeps checked-and-agrees distinguishable
    from never-examined (#112)."""
    tl = tracklist([_file(1, "Nightcall")], [_mb_track(1, "Nightcall")])

    assert tl.collapsed_summary.startswith("Artist, Artist sort and ")
    assert tl.collapsed_summary.endswith(" are the same on every track and match MusicBrainz")


def test_the_number_column_accounts_for_the_disc_on_a_multi_disc_release():
    """`_number` renders "2-4" there, so `disc_num` must not ALSO take a column —
    the same fact twice across one row. On a single-disc release it renders
    `disc or 1`, saying nothing about the disc, so the field stays eligible."""
    one_disc = tracklist([_file(1, "Nightcall")], [_mb_track(1, "Nightcall")])
    assert one_disc.shown_fields == {"track_num", "title"}

    two_discs = tracklist(
        [_file(1, "Nightcall", disc=1), _file(1, "Rampage", disc=2)],
        [_mb_track(1, "Nightcall", disc=1), _mb_track(1, "Rampage", disc=2)],
    )
    assert "disc_num" in two_discs.shown_fields
    assert "Disc no." not in _headings(two_discs)


def test_no_per_track_tag_is_in_two_places_or_in_none():
    """#309's invariant, over an album built to make each rule fire.

    Every per-track tag ends up in exactly one place: a column, the named
    collapsed set, or — by being in neither — the re-tag box. The failure this
    catches is a field claimed by `shown_fields` AND listed as collapsed, which
    reads on the page as a row printed twice: the exact complaint #297 was filed
    about, one level down.
    """
    tl = tracklist(
        [_file(1, "Nightcall"), _file(2, "Odd Look", artist_sort="Kavinsky, DJ", isrcs=["X"])],
        [_mb_track(1, "Nightcall"), _mb_track(2, "Odd Look")],
    )

    per_track = {f.value for f in Owned} - PANEL_FIELDS - {"mb_album_id"}
    collapsed = {c.label for c in tl.collapsed}
    columns = {c.label for c in tl.columns}
    assert not collapsed & columns
    # Nothing is missing either: what neither surface holds is what the box gets,
    # and every per-track tag is accounted for by one of the three.
    overflow = per_track - tl.shown_fields - {f.value for f in Owned if LABELS[f] in collapsed}
    assert overflow == set()


def test_only_id_rows_carry_a_musicbrainz_entity():
    """`entity` is what tells the page a value is an MBID rather than something
    to read, and it doubles as the path segment its link needs (#298).

    Asserted as the whole mapping rather than by sampling the two rows that have
    one: the failure worth catching is a field wrongly GAINING an entity — a
    date rendered as a link to `musicbrainz.org/artist/2019-03-15` — and that is
    invisible to a test which only checks that the two right ones are set.
    """
    fields = album_fields([("1.flac", TrackTags(album="Obreel"))], _tagset(album="Obreel"))

    assert {f.label: f.entity for f in fields if f.entity} == {
        "Album artist IDs": "artist",
        "Release group": "release-group",
    }


def test_only_credit_rows_are_marked_as_credits():
    """`credit` is what lets a value be redrawn as the artists it names (#309),
    and it is scoped to the fields that ARE credits rather than to any value that
    happens to match a credit phrase.

    Asserted as the whole set, for the reason above: the failure worth catching
    is a row wrongly gaining it — an album named after its artist turning its
    Album row into an artist link — which sampling the right ones cannot see.
    """
    fields = album_fields([("1.flac", TrackTags(album="Obreel"))], _tagset(album="Obreel"))

    assert {f.label for f in fields if f.credit} == {"Album artist"}
    # And the per-track half, which reaches the same table through a column.
    tl = tracklist(
        [_file(1, "Nightcall", artist="Kavinsky feat. Lovefoxxx")],
        [_mb_track(1, "Nightcall", artist="Kavinsky feat. Lovefoxxx")],
    )
    assert {f.label for f in tl.tracks[0].fields if f.credit} == {"Artist"}


def test_the_panel_pairs_release_fields_against_artist_fields():
    """#307. The grid fills row-major, two label/value pairs per row, so the
    field sequence decides which COLUMN each row lands in — and `Owned` order,
    which this used until now, was never chosen for that. It split the artist
    fields across both columns and the release fields across both, so reading
    down either one gave three subjects interleaved.

    Pinned as the whole sequence, because the property being asserted is about
    ADJACENCY: it lives in the pairs, and sampling two of them cannot see a
    third that has drifted into the wrong column.
    """
    fields = album_fields([("1.flac", TrackTags(album="Obreel"))], _tagset(album="Obreel"))

    assert [f.label for f in fields] == [
        "Album",           "Album artist",
        "Release group",   "Album artist sort",
        "Release type",    "Album artist IDs",
        "Release status",  "Country",
        "Date",            "Original date",
        "Label",           "Cat. no.",
        "Barcode",         "ASIN",
        "Disc total",      "Script",
        "Genre",           "Comment",
    ]  # fmt: skip


def test_display_order_cannot_drop_a_field():
    """The order is a sort key, not a second list of what to show.

    That distinction is the whole safety property. A hand-written list of rows
    is exactly what let this panel omit twenty-one fields (#295), and it is
    still hand-written here — so a field nobody remembered to place has to end
    up at the BOTTOM of the panel, never absent from it.

    `mb_album_id` is a real album field that `_DISPLAY_ORDER` genuinely does not
    name, so this asserts against the live gap rather than a fabricated one.
    """
    from harmonist.compare import _DISPLAY_ORDER, _in_display_order

    assert Owned.MB_ALBUM_ID not in _DISPLAY_ORDER

    placed = _in_display_order(list(ALBUM_FIELDS))
    assert set(placed) == set(ALBUM_FIELDS)  # nothing lost
    assert placed[-1] is Owned.MB_ALBUM_ID  # and the unplaced one is last
