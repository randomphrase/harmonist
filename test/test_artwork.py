"""What Harmonist can say about an album's artwork (#155).

Two layers here, and they fail for different reasons:

- `harmonist.images` reads a width and a height out of raw bytes, so its tests
  are over bytes and nothing else.
- `formats.read_tags` describes each file's embedded image from the handle it
  already has open. Parametrised over every fixture format, because "the art is
  already parsed" is a claim about mutagen that has to hold for ID3, MP4 atoms
  and FLAC picture blocks separately — one of them getting it wrong is exactly
  the kind of gap a single-format test would miss.
"""

from __future__ import annotations

import shutil
import struct
import zlib
from collections.abc import Sequence
from pathlib import Path

import pytest

from harmonist import artwork, formats, images
from harmonist.formats import TrackTags
from harmonist.formats.types import EmbeddedArt

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Every fixture format that can carry embedded art, as (extension, filename) —
# the same list `test_formats.py` parametrises over, for the same reason.
FIXTURES = [
    (".m4a", "sine.m4a"),
    (".mp3", "sine.mp3"),
    (".flac", "sine.flac"),
    (".opus", "sine.opus"),
]


def png_bytes(width: int, height: int) -> bytes:
    """A real, decodable PNG of the given size — built here rather than kept as
    a binary fixture so a test can ask for the dimensions it wants to assert."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x7f\x7f\x7f" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def jpeg_bytes(width: int, height: int, *, padding: int = 0) -> bytes:
    """A JPEG far enough along to be measurable: SOI, an APP0 of `padding`
    bytes, then the SOF0 frame header carrying the size.

    `padding` is the point of the parameter — the size sits behind however much
    metadata the encoder wrote, and a reader that assumed a fixed offset would
    pass on a bare header and fail on every real photo-derived cover.
    """
    app0 = b"\xff\xe0" + struct.pack(">H", padding + 2) + b"\x00" * padding
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


class TestDimensions:
    def test_reads_png_size(self) -> None:
        assert images.dimensions(png_bytes(120, 80)) == (120, 80)

    def test_reads_jpeg_size(self) -> None:
        assert images.dimensions(jpeg_bytes(1400, 1400)) == (1400, 1400)

    def test_reads_jpeg_size_behind_metadata(self) -> None:
        """The frame header sits behind whatever EXIF and colour profile the
        encoder wrote, so the walk has to follow the segment chain to find it."""
        assert images.dimensions(jpeg_bytes(600, 600, padding=4000)) == (600, 600)

    @pytest.mark.parametrize(
        "data",
        [
            pytest.param(b"", id="empty"),
            pytest.param(b"RIFF\x00\x00\x00\x00WEBPVP8 ", id="webp"),
            pytest.param(b"\xff\xd8\xff\xe0\x00\x10JFIF", id="truncated-jpeg"),
            pytest.param(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4, id="truncated-png"),
        ],
    )
    def test_unreadable_size_is_none_not_a_guess(self, data: bytes) -> None:
        """None is an answer the section renders around — format and byte size
        without dimensions. A guess here would put a wrong number on the page."""
        assert images.dimensions(data) is None


class TestEmbeddedArtOnRead:
    """`read_tags` describes the image from the open it already makes."""

    @pytest.fixture(params=FIXTURES, ids=[ext for ext, _ in FIXTURES])
    def track(self, request: pytest.FixtureRequest, tmp_path: Path) -> Path:
        ext, name = request.param
        dest = tmp_path / f"track{ext}"
        shutil.copy(FIXTURES_DIR / name, dest)
        return dest

    def test_no_art_reads_as_none(self, track: Path) -> None:
        assert formats.read_tags(track).art is None

    def test_describes_the_embedded_image(self, track: Path) -> None:
        cover = png_bytes(1400, 1400)
        formats.write_cover(track, cover)

        art = formats.read_tags(track).art

        assert art is not None
        assert art.size == (1400, 1400)
        assert art.length == len(cover)
        assert art.mime == "image/png"

    def test_digest_is_the_one_the_tagger_records(self, track: Path) -> None:
        """The album page compares tracks by this digest and the tagger decides
        whether to preserve per-track art by it. Two spellings of sha256 would
        let the page report a difference the re-tag then didn't act on."""
        cover = png_bytes(64, 64)
        formats.write_cover(track, cover)

        assert formats.read_tags(track).art.digest == images.digest(cover)

    def test_same_image_reads_identically_across_files(self, track: Path, tmp_path: Path) -> None:
        """The claim the whole section rests on: two tracks carrying one image
        are seen to carry one image."""
        other = tmp_path / f"other{track.suffix}"
        shutil.copy(track, other)
        cover = png_bytes(300, 300)
        formats.write_cover(track, cover)
        formats.write_cover(other, cover)

        assert formats.read_tags(track).art.digest == formats.read_tags(other).art.digest

    def test_different_images_are_distinguished(self, track: Path, tmp_path: Path) -> None:
        other = tmp_path / f"other{track.suffix}"
        shutil.copy(track, other)
        formats.write_cover(track, png_bytes(300, 300))
        formats.write_cover(other, png_bytes(301, 301))

        assert formats.read_tags(track).art.digest != formats.read_tags(other).art.digest


# ---------------------------------------------------------------------------
# What the section says about an album — `harmonist.artwork` (#155)
# ---------------------------------------------------------------------------


def art_of(seed: int, *, width: int = 1400, height: int = 1400) -> EmbeddedArt:
    """A distinct image, described. `seed` is what makes two of them differ."""
    data = png_bytes(width, height) + bytes([seed])
    return EmbeddedArt.of(data, "image/png")


def cover_of(image: EmbeddedArt, name: str = "cover.jpg") -> artwork.FolderCover:
    return artwork.FolderCover(name=name, image=image)


def album(
    *art: EmbeddedArt | None, disc: int | None = None, titles: Sequence[str] = ()
) -> list[tuple[str, TrackTags]]:
    """An album of `(file_name, tags)`, one entry per track, numbered from 1."""
    return [
        (
            f"{i:02d} Track.m4a",
            TrackTags(
                art=a,
                track_num=i,
                disc_num=disc,
                title=titles[i - 1] if i <= len(titles) else None,
            ),
        )
        for i, a in enumerate(art, start=1)
    ]


class TestOutcomes:
    """What a re-tag would do, per row — and which rows say nothing at all."""

    def test_one_image_everywhere_is_a_single_row_that_writes_nothing(self) -> None:
        """Tracks and folder cover carrying one picture is ONE thing to look at,
        not a row plus an incoming copy of itself (#400)."""
        one = art_of(1)
        view = artwork.summarise(album(one, one, one), cover_of(one))

        assert len(view.rows) == 1
        assert view.rows[0].label == "All 3 tracks and cover.jpg"
        assert view.writes is False
        assert view.rows[0].writes is False

    def test_a_differing_folder_cover_gets_its_own_row(self) -> None:
        """It is a file the album HAS. Showing it only as an incoming value read
        as something arriving from outside, and left the reader asking which of
        the two images was about to be replaced (#400).

        A larger cover, so it is the one that wins and the row really is
        replaced — an equal one is left alone since #397."""
        view = artwork.summarise(
            album(art_of(1, width=400, height=400), art_of(1, width=400, height=400)),
            cover_of(art_of(2, width=900, height=900)),
        )

        assert [r.label for r in view.rows] == ["All 2 tracks", "cover.jpg"]
        assert [r.outcome for r in view.rows] == [artwork.Outcome.REPLACED, artwork.Outcome.SAME]
        assert [r.writes for r in view.rows] == [True, False]

    def test_per_track_art_is_kept_and_says_nothing(self) -> None:
        """A compilation's covers are preserved, so no row may offer a
        replacement — and none may spend a column saying it doesn't."""
        view = artwork.summarise(album(art_of(1), art_of(2), art_of(3)), cover_of(art_of(9)))

        # The three track rows are preserved; the fourth is the folder cover,
        # which is a file the album has and which nothing writes to either.
        assert {r.outcome for r in view.rows if r.tracks} == {artwork.Outcome.KEPT}
        assert not any(r.writes for r in view.rows)
        assert view.writes is False

    def test_a_gap_is_filled_without_rewriting_the_tracks_that_are_right(self) -> None:
        """#397: the holes are filled from the album's own image, and the tracks
        that already carry it are left alone. The folder cover here is no better
        — same size, different picture — so it wins nothing."""
        one = art_of(1)
        view = artwork.summarise(album(one, None, one, None), cover_of(art_of(2)))

        outcomes = {r.is_gap: r.outcome for r in view.rows if r.tracks}
        assert outcomes == {False: artwork.Outcome.KEPT, True: artwork.Outcome.FILLED}
        gap = next(r for r in view.rows if r.is_gap)
        assert gap.written_from == "the album's own artwork"

    def test_a_better_folder_cover_does_replace_the_tracks(self) -> None:
        """The other direction, and the reason the rule is about size rather
        than about never touching what is there."""
        small = art_of(1, width=400, height=400)
        view = artwork.summarise(album(small, None), cover_of(art_of(2, width=900, height=900)))

        outcomes = {r.is_gap: r.outcome for r in view.rows if r.tracks}
        assert outcomes == {False: artwork.Outcome.REPLACED, True: artwork.Outcome.FILLED}

    def test_no_folder_cover_means_nothing_is_written(self) -> None:
        view = artwork.summarise(album(art_of(1), None), None)

        assert {r.outcome for r in view.rows} == {artwork.Outcome.KEPT}
        assert view.writes is False

    def test_outcomes_follow_the_taggers_own_verdict(self) -> None:
        """`preserves_per_track_art` in `tagger._prepare` and the KEPT rows here
        are the same decision, reached through one predicate."""
        digests = [art_of(1).digest, art_of(2).digest]
        assert artwork.has_per_track_art(digests) is True
        assert artwork.has_per_track_art([digests[0], digests[0], None]) is False


class TestLabels:
    def test_every_track_reads_as_all_of_them(self) -> None:
        one = art_of(1)
        assert artwork.summarise(album(one, one, one), None).rows[0].label == "All 3 tracks"

    def test_some_tracks_are_named_by_number(self) -> None:
        one, two = art_of(1), art_of(2)
        rows = artwork.summarise(album(one, two, one), None).rows
        assert [r.label for r in rows] == ["Tracks 1, 3", "Track 2"]

    def test_consecutive_tracks_collapse_to_a_range(self) -> None:
        one, two = art_of(1), art_of(2)
        assert artwork.summarise(album(one, one, one, two), None).rows[0].label == "Tracks 1–3"

    def test_a_multi_disc_album_names_the_disc(self) -> None:
        """A bare track number is not a unique reference across discs, and
        rendering one is wrong rather than merely terse (#400)."""
        one, two = art_of(1), art_of(2)
        tracks = album(one, two, disc=1) + album(one, disc=2)

        rows = artwork.summarise(tracks, None).rows

        assert rows[0].label == "Disc 1, track 1 · Disc 2, track 1"
        assert rows[1].label == "Disc 1, track 2"

    def test_a_single_disc_album_does_not_mention_discs(self) -> None:
        one, two = art_of(1), art_of(2)
        rows = artwork.summarise(album(one, two, disc=1), None).rows
        assert [r.label for r in rows] == ["Track 1", "Track 2"]

    def test_a_row_of_one_track_carries_its_title(self) -> None:
        """A number is a poor thing to recognise an image by; on a box set of
        episodes the title is how you know which one this is."""
        one, two = art_of(1), art_of(2)
        view = artwork.summarise(album(one, two, titles=["Dexter's Chalk", "Appleshine"]), None)

        assert view.rows[0].title == "Dexter's Chalk"

    def test_a_row_of_several_tracks_carries_no_title(self) -> None:
        one = art_of(1)
        view = artwork.summarise(album(one, one, titles=["A", "B"]), None)
        assert view.rows[0].title is None

    def test_unnumbered_tracks_fall_back_to_a_count(self) -> None:
        one, two = art_of(1), art_of(2)
        tracks = [
            ("a.m4a", TrackTags(art=one)),
            ("b.m4a", TrackTags(art=one)),
            ("c.m4a", TrackTags(art=two)),
        ]
        assert artwork.summarise(tracks, None).rows[0].label == "2 tracks"


class TestRows:
    def test_the_gap_row_names_the_files(self) -> None:
        """The remedy is per file, so a count would not be enough to act on."""
        one = art_of(1)
        view = artwork.summarise(album(one, None), cover_of(one))
        gap = next(r for r in view.rows if r.is_gap)
        assert [t.name for t in gap.tracks] == ["02 Track.m4a"]

    def test_a_cover_no_track_carries_is_a_row_with_no_tracks(self) -> None:
        view = artwork.summarise(album(None, None), cover_of(art_of(1)))
        cover_row = next(r for r in view.rows if r.on_cover)
        assert cover_row.tracks == ()
        assert cover_row.label == "cover.jpg"

    def test_unreadable_files_vote_for_nothing(self) -> None:
        """A file Harmonist cannot open is not a file without artwork (#112)."""
        one = art_of(1)
        tracks = album(one, one)
        tracks.append(("03 Track.m4a", TrackTags(unreadable=True)))

        view = artwork.summarise(tracks, cover_of(one))

        assert view.unreadable == 1
        assert not any(r.is_gap for r in view.rows)
        # Not "All 3 tracks": the third was never read, so the row can't claim it.
        assert view.rows[0].label == "Tracks 1–2 and cover.jpg"


class TestHeadingCount:
    def test_counts_the_distinct_images(self) -> None:
        view = artwork.summarise(album(art_of(1), art_of(2)), cover_of(art_of(3)))
        assert view.count == "3 images"

    def test_counts_gaps_alongside(self) -> None:
        """ "1 image" over an album a third of whose tracks have none is true and
        misses the point."""
        one = art_of(1)
        view = artwork.summarise(album(one, None, one), cover_of(one))
        assert view.count == "1 image · 1 gap"

    def test_no_count_when_there_is_no_image_anywhere(self) -> None:
        assert artwork.summarise(album(None, None), None).count is None


class TestLargerLocalImageWins:
    """The page must reach the same verdict as `tagger._prepare` (#410)."""

    def test_a_bigger_album_image_replaces_the_folder_cover_not_the_tracks(self) -> None:
        big = art_of(1, width=1000, height=1000)
        small = art_of(2, width=400, height=400)

        view = artwork.summarise(album(big, big), cover_of(small))

        tracks_row, cover_row = view.rows
        assert tracks_row.outcome is artwork.Outcome.KEPT
        assert cover_row.outcome is artwork.Outcome.REPLACED
        assert cover_row.written_from == "the album's own artwork"

    def test_a_bigger_folder_cover_still_replaces_the_tracks(self) -> None:
        small = art_of(1, width=400, height=400)
        big = art_of(2, width=1000, height=1000)

        view = artwork.summarise(album(small, small), cover_of(big))

        assert view.rows[0].outcome is artwork.Outcome.REPLACED
        assert view.rows[0].written_from == "cover.jpg"

    def test_an_unmeasurable_image_never_wins(self) -> None:
        """A guess is not worth overwriting a user's cover for, in either
        direction — `beats` says no when either size is unknown."""
        unknown = EmbeddedArt.of(b"not an image at all", "image/jpeg")
        assert unknown.size is None

        view = artwork.summarise(album(unknown, unknown), cover_of(art_of(2, width=40, height=40)))

        # Falls back to today's behaviour: the folder cover is embedded.
        assert view.rows[0].outcome is artwork.Outcome.REPLACED

    def test_equal_sizes_change_nothing(self) -> None:
        """Neither image wins, so neither file is written: a same-sized
        different picture is not an improvement in either direction (#397)."""
        mine = art_of(1, width=500, height=500)
        theirs = art_of(2, width=500, height=500)

        view = artwork.summarise(album(mine, mine), cover_of(theirs))

        assert not any(r.writes for r in view.rows)
        assert view.rows[0].outcome is artwork.Outcome.KEPT

    def test_per_track_art_is_never_promoted(self) -> None:
        view = artwork.summarise(
            album(art_of(1, width=900, height=900), art_of(2, width=900, height=900)),
            cover_of(art_of(3, width=100, height=100)),
        )

        assert not any(r.writes for r in view.rows)


class TestCoverArtArchiveNote:
    """What the section says once the archive has been asked (#276)."""

    class _Answer:
        def __init__(self, width=None, height=None, art=True):
            self.width, self.height, self._art = width, height, art

        @property
        def has_art(self) -> bool:
            return self._art

    def test_nothing_said_until_it_has_been_asked(self) -> None:
        assert artwork.summarise(album(art_of(1)), None).caa_note is None

    def test_a_larger_archive_cover_is_worth_saying(self) -> None:
        view = artwork.summarise(
            album(art_of(1, width=600, height=600)), None, self._Answer(1400, 1400)
        )
        assert view.caa_note == "The Cover Art Archive has a larger front cover: 1400×1400."

    def test_measured_against_the_best_image_the_album_has(self) -> None:
        """Not against the folder cover alone: an album whose tracks carry
        3000px is not improved by a 2000px archive cover just because its
        cover.jpg is smaller still."""
        big = art_of(1, width=3000, height=3000)
        small = art_of(2, width=500, height=500)

        view = artwork.summarise(album(big), cover_of(small), self._Answer(2000, 2000))

        assert view.caa_note is not None
        assert "no better than what this album already has" in view.caa_note

    def test_the_archive_having_nothing_is_reported(self) -> None:
        view = artwork.summarise(album(art_of(1)), None, self._Answer(art=False))
        assert view.caa_note == "The Cover Art Archive has no front cover for this release."

    def test_an_unmeasurable_archive_cover_says_so(self) -> None:
        view = artwork.summarise(album(art_of(1)), None, self._Answer())
        assert view.caa_note is not None
        assert "size could not be read" in view.caa_note
