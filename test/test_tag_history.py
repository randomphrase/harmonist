"""Field-first aggregation of stored tag-change records (#86).

Pure functions over values, so these tests build `TagChanges` directly rather
than tagging real files — what's under test is the inversion from one-row-per-
file to one-row-per-field, not the recording.
"""

from __future__ import annotations

from harmonist.activity_store import TagChanges
from harmonist.formats.owned import ARTWORK, Scope
from harmonist.tag_history import display, summarise


def _album(n: int, changes_for) -> list[TagChanges]:
    """`n` files, each with whatever `changes_for(i)` returns (1-based)."""
    return [
        TagChanges(file=f"{i:02d} track.flac", changes=changes_for(i), position=str(i))
        for i in range(1, n + 1)
    ]


def _row(rows, field):
    return next(r for r in rows if r.field == field)


def test_a_change_on_every_track_becomes_one_row():
    """The point of the whole module: 18 files that all changed the same way
    are one line, not 18. A layout whose height scales with the tracklist is
    unusable exactly when it's doing the most work (#32's nightly runs)."""
    records = _album(18, lambda i: {"artist": ["Boards Of Canada", "Boards of Canada"]})

    rows = summarise(records)

    assert len(rows) == 1
    assert rows[0].before == "Boards Of Canada"
    assert rows[0].after == "Boards of Canada"
    assert rows[0].tracks == 18
    assert rows[0].uniform


def test_reach_distinguishes_album_wide_from_partial_and_per_track():
    """The annotation beside each value, and the reason the per-album/per-track
    split from #149 is visible rather than plumbing."""
    records = _album(
        18,
        lambda i: (
            {"label": [None, "Warp Records"], "artist": ["A", "B"]}
            | ({"title": ["ROYGBIV", "Roygbiv"]} if i == 9 else {})
        ),
    )

    rows = summarise(records)

    # An album-scoped field that moved everywhere is ONE change — saying "all 18
    # tracks" would invite the reader to go and check 18 things.
    assert _row(rows, "label").reach == "album"
    assert _row(rows, "label").scope is Scope.ALBUM
    # A track-scoped field that moved everywhere did move on every track.
    assert _row(rows, "artist").reach == "all tracks"
    assert _row(rows, "artist").scope is Scope.TRACK
    # Partial always gets the count, whatever the scope: this is the case where
    # the album has no single answer.
    assert _row(rows, "title").reach == "1 of 18 tracks"


def test_album_fields_sort_before_track_fields_and_artwork_sits_last():
    records = _album(2, lambda i: {"title": ["a", "b"], "label": [None, "L"], ARTWORK: ["x", "y"]})

    assert [r.field for r in summarise(records)] == ["label", "title", ARTWORK]


def test_a_field_that_changed_differently_per_track_keeps_every_version():
    """A compilation where each track's artist changed to something different
    has no album-wide answer."""
    records = _album(4, lambda i: {"artist": [f"old {i}", f"new {i}"]})

    row = _row(summarise(records), "artist")

    assert not row.uniform
    assert len(row.variants) == 4
    assert row.variants[0].before == "old 1"
    assert row.variants[0].position == "1"


def test_a_field_whose_files_disagree_counts_the_answers_instead_of_picking_one():
    """#185: 38 tracks legitimately carry 38 different recording MBIDs, and
    printing the first one beside "all tracks" reads as though the whole album
    got it. The reach can't take that back — it says the same thing whether the
    value was the same on every track or different on every track."""
    records = _album(38, lambda i: {"mb_track_id": [None, f"mbid-{i}"]})

    row = _row(summarise(records), "mb_track_id")

    assert row.varied_summary == "38 different values"
    assert row.reach == "all tracks"
    # No emphasis runs either: there is no pair on show to emphasise.
    assert row.before_runs == () and row.after_runs == ()


def test_the_answer_count_is_of_distinct_values_not_of_files():
    """Four tracks that moved two different ways is two answers, not four —
    the count says how many things happened, not how many files there were."""
    records = _album(4, lambda i: {"artist": ["A", "X" if i < 3 else "Y"]})

    assert _row(summarise(records), "artist").varied_summary == "2 different values"


def test_a_uniform_field_still_shows_its_value():
    """The suppression is for disagreement only. An album that moved as one has
    exactly one answer, and hiding it behind a count would make the common case
    unreadable to save the rare one."""
    row = _row(summarise(_album(18, lambda i: {"artist": ["A", "B"]})), "artist")

    assert row.uniform
    assert (row.before, row.after) == ("A", "B")


def test_an_even_split_resolves_to_the_first_files_pair():
    """The tie rule behind `_representative`, which since #185 the value cell no
    longer consults — it survives for the artwork row below. Kept the same rule
    `compare.consensus` uses on the album panel: a reader who learned it there
    must not find a different one here."""
    records = [
        TagChanges(file="01.flac", changes={"artist": ["A", "X"]}, position="1"),
        TagChanges(file="02.flac", changes={"artist": ["B", "Y"]}, position="2"),
        TagChanges(file="03.flac", changes={"artist": ["B", "Y"]}, position="3"),
        TagChanges(file="04.flac", changes={"artist": ["A", "X"]}, position="4"),
    ]

    row = _row(summarise(records), "artist")

    assert (row.before, row.after) == ("A", "X")


def test_the_representative_pair_is_a_transition_some_track_actually_made():
    """Resolving before and after independently could manufacture a change no
    file made. Here 'A' is the commonest before and 'Z' the commonest after,
    but no track went A -> Z."""
    records = [
        TagChanges(file="01.flac", changes={"artist": ["A", "X"]}, position="1"),
        TagChanges(file="02.flac", changes={"artist": ["A", "Y"]}, position="2"),
        TagChanges(file="03.flac", changes={"artist": ["B", "Z"]}, position="3"),
        TagChanges(file="04.flac", changes={"artist": ["C", "Z"]}, position="4"),
    ]

    row = _row(summarise(records), "artist")

    assert (row.before, row.after) in {("A", "X"), ("A", "Y"), ("B", "Z"), ("C", "Z")}


def test_the_artwork_row_keeps_the_image_most_of_the_album_lost():
    """Artwork is the one row that still needs a representative on a disagreeing
    album: it shows what happened rather than a value, and `before` is what
    decides whether the restore button is offered (#131). A track that never had
    an image must not talk the other three out of getting theirs back."""
    records = [
        TagChanges(file="01.flac", changes={ARTWORK: [None, "b" * 64]}, position="1"),
        *(
            TagChanges(file=f"0{i}.flac", changes={ARTWORK: ["a" * 64, "b" * 64]}, position=str(i))
            for i in (2, 3, 4)
        ),
    ]

    row = _row(summarise(records), ARTWORK)

    assert not row.uniform
    assert row.before == "a" * 64


def test_artwork_states_what_happened_instead_of_reciting_digests():
    """The digests are for #131 to find the images by, not for a person to
    read. Two sha256s would dominate every row around them and say nothing."""
    replaced = _row(summarise(_album(1, lambda i: {ARTWORK: ["a" * 64, "b" * 64]})), ARTWORK)
    added = _row(summarise(_album(1, lambda i: {ARTWORK: [None, "b" * 64]})), ARTWORK)

    assert replaced.opaque and replaced.summary == "replaced"
    assert added.opaque and added.summary == "added"
    # No in-value emphasis either: two digests differ nearly everywhere, so
    # marking the runs would be noise on top of noise.
    assert replaced.before_runs == ()
    # Artwork is not an owned field, so it has no album/track scope of its own.
    assert replaced.scope is None


def test_an_unknown_field_renders_under_its_own_name_rather_than_raising():
    """These records are permanent and unversioned: a row written today may name
    a field a later build has renamed or dropped. One old row must not take a
    whole album page down."""
    records = [TagChanges(file="01.flac", changes={"some_future_field": ["old", "new"]})]

    row = summarise(records)[0]

    assert row.label == "some_future_field"
    assert (row.before, row.after) == ("old", "new")
    assert row.scope is None


def test_a_malformed_entry_is_skipped_and_its_neighbours_survive():
    records = [TagChanges(file="01.flac", changes={"artist": "not-a-pair", "title": ["a", "b"]})]

    rows = summarise(records)

    assert [r.field for r in rows] == ["title"]


def test_absent_values_collapse_the_way_the_tagger_collapses_them():
    """None, "" and [] all mean "not there" — the record keeps them raw so a
    revert restores exactly what was there, but the reader must not draw a
    distinction the tagger doesn't make."""
    assert display(None) is None
    assert display("") is None
    assert display([]) is None
    assert display(["a", "b"]) == "a; b"
    assert display(9) == "9"


def test_nothing_recorded_summarises_to_nothing():
    assert summarise([]) == ()
