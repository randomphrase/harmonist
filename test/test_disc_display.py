"""Discs are shown as discs, with their names (#216).

The album page's tracklist had no notion of a disc: a multi-disc release was one
flat list, and the only hint of structure was a `2-4` prefix on each row.

For Midnight Oil's *Best of Both Worlds* — a 44-track DVD the user never ripped
plus the 16-track CD they did — that produced a headline of "44 of 60 tracks
differ from MusicBrainz · 44 not on disk" above forty-four individual "Not on
disk" rows, burying the sixteen tracks they actually have.
"""

from __future__ import annotations

from harmonist import compare
from harmonist.compare import MBTrack, Medium, TrackState
from harmonist.formats.types import TagSet, TrackTags


def _mb(disc: int, n: int, title: str, total: int) -> MBTrack:
    return MBTrack(
        tags=TagSet(
            title=title,
            album="A",
            artist="X",
            mb_album_id="rel",
            album_artist="X",
            track_total=total,
            disc_num=disc,
            track_num=n,
        ),
        length_ms=200_000,
    )


def _file(
    n: int, disc: int, title: str, total: int = 16, media: str | None = None
) -> tuple[str, TrackTags]:
    """A file the matching `_mb` track agrees with completely.

    `owned` carries the snapshot a real `read_tags` takes, `track_total`
    included — since #309 a per-track tag the file lacks and MusicBrainz has is a
    difference the tracklist reports as its own column, so a fixture short of one
    makes every album here differ on it.

    `media` is the same trap one surface further out: since #320 the disc heading
    compares it, and a disc that differs gets a clause in the headline these
    tests assert on. Passed by the caller, because the value that agrees is
    whatever `Medium` the caller handed the comparison.
    """
    return (
        f"{disc}-{n:02d}.m4a",
        TrackTags(
            title=title,
            album="A",
            artist="X",
            disc_num=disc,
            track_num=n,
            duration_ms=200_000,
            owned={
                "title": title,
                "album": "A",
                "artist": "X",
                "album_artist": "X",
                "disc_num": disc,
                "track_num": n,
                "track_total": total,
                "media": media,
            },
        ),
    )


def _two_disc(present_disc: int = 2):
    """A 44-track disc 1 and a 16-track disc 2; only `present_disc` on disk.

    The media match the `Medium`s every caller passes, so the only finding in
    this fixture is the absent disc these tests are about.
    """
    mb = [_mb(1, i, f"Video {i}", 44) for i in range(1, 45)]
    mb += [_mb(2, i, f"Song {i}", 16) for i in range(1, 17)]
    files = (
        [_file(i, 2, f"Song {i}", media="CD") for i in range(1, 17)]
        if present_disc == 2
        else [_file(i, 1, f"Video {i}", 44, media="DVD-Video") for i in range(1, 45)]
    )
    return files, mb


def test_tracks_are_grouped_by_disc():
    files, mb = _two_disc()
    t = compare.tracklist(files, mb, [Medium(1, None, "DVD-Video"), Medium(2, None, "CD")])

    assert [g.medium.position for g in t.discs] == [1, 2]
    assert [len(g.tracks) for g in t.discs] == [44, 16]


def test_a_disc_is_named_when_musicbrainz_names_it():
    """MusicBrainz calls Hybrid's two discs Wide Angle and Live Angle, which is
    a good deal more use than "Disc 1" and "Disc 2"."""
    mb = [_mb(1, i, f"T{i}", 2) for i in (1, 2)] + [_mb(2, i, f"U{i}", 2) for i in (1, 2)]
    files = [_file(i, 1, f"T{i}", 2) for i in (1, 2)] + [_file(i, 2, f"U{i}", 2) for i in (1, 2)]

    t = compare.tracklist(files, mb, [Medium(1, "Wide Angle"), Medium(2, "Live Angle")])

    assert [g.medium.label for g in t.discs] == ["Disc 1 — Wide Angle", "Disc 2 — Live Angle"]


def test_a_disc_without_a_name_falls_back_to_its_number():
    mb = [_mb(1, i, f"T{i}", 2) for i in (1, 2)]
    files = [_file(i, 1, f"T{i}", 2) for i in (1, 2)]

    t = compare.tracklist(files, mb, [Medium(1, None, "CD")])

    assert t.discs[0].medium.label == "Disc 1"


def test_a_single_disc_album_is_one_group():
    """The template renders no heading for it — nearly every album is one disc,
    and a heading above the only disc is noise."""
    mb = [_mb(1, i, f"T{i}", 3) for i in (1, 2, 3)]
    files = [_file(i, 1, f"T{i}", 3) for i in (1, 2, 3)]

    assert len(compare.tracklist(files, mb).discs) == 1


def test_an_entirely_absent_disc_is_marked_absent():
    files, mb = _two_disc(present_disc=2)
    t = compare.tracklist(files, mb, [Medium(1, None, "DVD-Video"), Medium(2, None, "CD")])

    disc1, disc2 = t.discs
    assert disc1.absent is True
    assert disc2.absent is False
    assert disc1.summary == "DVD-Video, 44 tracks"


def test_a_partly_present_disc_is_not_absent():
    """`absent` means NOT ONE track is here. A short disc is a different thing,
    with a different remedy, and still gets its tracks listed."""
    mb = [_mb(1, i, f"T{i}", 4) for i in range(1, 5)]
    files = [_file(i, 1, f"T{i}", 4) for i in (1, 2)]

    assert compare.tracklist(files, mb, [Medium(1)]).discs[0].absent is False


def test_the_headline_reports_an_absent_disc_once_not_track_by_track():
    """The Midnight Oil case. It used to read "44 of 60 tracks differ from
    MusicBrainz · 44 not on disk", which made a bonus DVD the user knowingly
    never ripped into the album's dominant problem."""
    files, mb = _two_disc()
    t = compare.tracklist(files, mb, [Medium(1, None, "DVD-Video"), Medium(2, None, "CD")])

    assert t.summary == "All 16 tracks match MusicBrainz · Disc 1 not on disk"


def test_an_absent_named_disc_is_named_in_the_headline():
    mb = [_mb(1, i, f"T{i}", 2) for i in (1, 2)] + [_mb(2, i, f"U{i}", 2) for i in (1, 2)]
    files = [_file(i, 2, f"U{i}", 2) for i in (1, 2)]

    t = compare.tracklist(files, mb, [Medium(1, "Bonus DVD"), Medium(2, "Album")])

    assert "Disc 1 — Bonus DVD not on disk" in t.summary


def test_a_genuinely_short_disc_still_counts_its_missing_tracks():
    """The control. Suppressing per-track counts is only right for a disc that
    is ENTIRELY absent; a half-ripped one is a real defect to report."""
    mb = [_mb(1, i, f"T{i}", 4) for i in range(1, 5)]
    files = [_file(i, 1, f"T{i}", 4) for i in (1, 2)]

    summary = compare.tracklist(files, mb, [Medium(1)]).summary

    assert "2 not on disk" in summary
    assert "not on disk" in summary and "Disc 1 not on disk" not in summary


def test_a_single_disc_album_headline_is_unchanged():
    mb = [_mb(1, i, f"T{i}", 3) for i in (1, 2, 3)]
    files = [_file(i, 1, f"T{i}", 3) for i in (1, 2, 3)]

    assert compare.tracklist(files, mb).summary == "All 3 tracks match MusicBrainz"


def test_every_row_knows_its_disc():
    """Carried structurally rather than left encoded in the rendered "2-4"
    position string, which is what makes grouping possible at all."""
    files, mb = _two_disc()
    t = compare.tracklist(files, mb, [Medium(1), Medium(2)])

    assert {row.disc for row in t.tracks} == {1, 2}
    assert all(r.state is TrackState.MISSING for r in t.tracks if r.disc == 1)
