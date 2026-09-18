"""Discs are shown as discs, with their names (#216).

The album page's tracklist had no notion of a disc: a multi-disc release was one
flat list, and the only hint of structure was a `2-4` prefix on each row.

For Midnight Oil's *Best of Both Worlds* — a 44-track DVD the user never ripped
plus the 16-track CD they did — that produced a headline of "44 of 60 tracks
differ from MusicBrainz · 44 not in your files" above forty-four individual
"Not in your files" rows, burying the sixteen tracks they actually have.
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
    MusicBrainz · 44 not in your files", which made a bonus DVD the user knowingly
    never ripped into the album's dominant problem."""
    files, mb = _two_disc()
    t = compare.tracklist(files, mb, [Medium(1, None, "DVD-Video"), Medium(2, None, "CD")])

    assert t.summary == "All 16 tracks match · Disc 1 not in your files"


def test_a_genuinely_short_disc_still_counts_its_missing_tracks():
    """The control. Suppressing per-track counts is only right for a disc that
    is ENTIRELY absent; a half-ripped one is a real defect to report."""
    mb = [_mb(1, i, f"T{i}", 4) for i in range(1, 5)]
    files = [_file(i, 1, f"T{i}", 4) for i in (1, 2)]

    summary = compare.tracklist(files, mb, [Medium(1)]).summary

    assert "2 not in your files" in summary
    assert "not in your files" in summary and "Disc 1 not in your files" not in summary


def test_a_single_disc_album_headline_is_unchanged():
    mb = [_mb(1, i, f"T{i}", 3) for i in (1, 2, 3)]
    files = [_file(i, 1, f"T{i}", 3) for i in (1, 2, 3)]

    assert compare.tracklist(files, mb).summary == "All 3 tracks match"


def test_every_row_knows_its_disc():
    """Carried structurally rather than left encoded in the rendered "2-4"
    position string, which is what makes grouping possible at all."""
    files, mb = _two_disc()
    t = compare.tracklist(files, mb, [Medium(1), Medium(2)])

    assert {row.disc for row in t.tracks} == {1, 2}
    assert all(r.state is TrackState.MISSING for r in t.tracks if r.disc == 1)


def _extra(n: int, disc: int, title: str, subtitle: str | None = None, media: str | None = None):
    """A file on a disc the release doesn't have (#402).

    Nothing pairs it with a MusicBrainz track — no release track id, and a disc
    number no medium answers to — so it lands in `extras`, and its own tags are
    the only account of which disc it is on.
    """
    return (
        f"{disc}-{n:02d}.m4a",
        TrackTags(
            title=title,
            album="A",
            artist="X",
            disc_num=disc,
            track_num=n,
            media=media,
            duration_ms=200_000,
            owned={
                "title": title,
                "album": "A",
                "artist": "X",
                "album_artist": "X",
                "disc_num": disc,
                "track_num": n,
                "disc_subtitle": subtitle,
                "media": media,
            },
        ),
    )


def _drift():
    """The DRIFT case: a two-disc release plus a Blu-ray ripped separately.

    MusicBrainz's release is the digital download, and it has no Blu-ray — so
    disc 3 here is a disc the release does not have, described by nothing but
    the files that claim it.
    """
    mb = [_mb(1, i, f"Dust {i}", 2) for i in (1, 2)] + [_mb(2, i, f"Rust {i}", 2) for i in (1, 2)]
    files = [_file(i, 1, f"Dust {i}", 2, media="Digital Media") for i in (1, 2)]
    files += [_file(i, 2, f"Rust {i}", 2, media="Digital Media") for i in (1, 2)]
    files += [_extra(i, 3, f"Trailer {i}", subtitle="Blu-ray", media="Blu-ray") for i in (1, 2)]
    return files, mb


def test_a_disc_the_release_does_not_have_gets_its_own_group():
    """It used to fall through to disc 1, which read as though Ep1 Dust were
    fifteen tracks long with a video trailer in the middle of it (#402)."""
    files, mb = _drift()
    t = compare.tracklist(files, mb, [Medium(1, "Ep1 Dust"), Medium(2, "Ep2 Rust")])

    assert [g.medium.position for g in t.discs] == [1, 2, 3]
    assert [len(g.tracks) for g in t.discs] == [2, 2, 2]
    assert all(r.state is TrackState.EXTRA for r in t.discs[2].tracks)


def test_that_disc_is_named_and_described_by_the_files():
    """MusicBrainz has nothing to say about it, and the files do: they carry a
    disc subtitle and a media, which is exactly what a heading states."""
    files, mb = _drift()
    t = compare.tracklist(files, mb, [Medium(1, "Ep1 Dust"), Medium(2, "Ep2 Rust")])

    assert t.discs[2].medium.label == "Disc 3 — Blu-ray"
    assert t.discs[2].summary == "Blu-ray, 2 tracks"


def test_that_disc_is_marked_as_the_one_musicbrainz_does_not_have():
    files, mb = _drift()
    t = compare.tracklist(files, mb, [Medium(1, "Ep1 Dust"), Medium(2, "Ep2 Rust")])

    assert [g.unknown for g in t.discs] == [False, False, True]


def test_the_files_only_name_a_disc_they_all_agree_about():
    """#112's rule, one surface along: two files disagreeing about what disc 3
    is called is not a name, and inventing one from the first file would report
    a tag the other file contradicts."""
    files, mb = _drift()
    files[-1] = _extra(2, 3, "Trailer 2", subtitle="Bonus", media="Blu-ray")
    t = compare.tracklist(files, mb, [Medium(1, "Ep1 Dust"), Medium(2, "Ep2 Rust")])

    assert t.discs[2].medium.label == "Disc 3"
    assert t.discs[2].summary == "Blu-ray, 2 tracks"


def test_a_bonus_track_stays_on_the_disc_it_says_it_is_on():
    """The ordinary extra — a hidden track on a disc the release DOES have.
    It joins that disc's group and is not marked as a disc MusicBrainz lacks."""
    files, mb = _drift()
    files.append(_extra(3, 2, "Hidden", media="Digital Media"))
    t = compare.tracklist(files, mb, [Medium(1, "Ep1 Dust"), Medium(2, "Ep2 Rust")])

    assert [len(g.tracks) for g in t.discs] == [2, 3, 2]
    assert [g.unknown for g in t.discs] == [False, False, True]


def test_no_disc_is_unknown_when_the_caller_gave_no_media():
    """A caller with no media to give leaves the discs grouped and unnamed — it
    has said nothing about which discs the release has, so nothing here may
    claim the release is missing one."""
    files, mb = _drift()
    t = compare.tracklist(files, mb)

    assert [g.medium.position for g in t.discs] == [1, 2, 3]
    assert not any(g.unknown for g in t.discs)
