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
        def __init__(self, width=None, height=None, art=True, length=718000, mime="image/jpeg"):
            self.width, self.height, self._art = width, height, art
            self.length, self.mime = length, mime

        @property
        def has_art(self) -> bool:
            return self._art

        @property
        def from_release_group(self) -> bool:
            return False

    def test_nothing_said_until_it_has_been_asked(self) -> None:
        assert artwork.summarise(album(art_of(1)), None).archive_row is None

    def test_a_winning_archive_cover_needs_no_sentence(self) -> None:
        """It is the incoming value, shown in that column with the hexagon —
        saying it in prose as well would say it twice (#433)."""
        # The cached IMAGE as well as the answer: an archive cover that was
        # never downloaded cannot be written, so it cannot win — the tagger
        # reads the same cache.
        view = artwork.summarise(
            album(art_of(1, width=600, height=600)),
            cover_of(art_of(2, width=600, height=600)),
            self._Answer(1400, 1400),
            archive=art_of(3, width=1400, height=1400),
        )
        assert view.archive_row is None  # not an also-ran; it won

    def test_a_losing_archive_cover_is_a_row_of_facts_not_a_sentence(self) -> None:
        """Drawn as the candidate it is, muted, with no picture — a losing image
        is never downloaded, so there genuinely is none to show (#433)."""
        big = art_of(1, width=3000, height=3000)
        small = art_of(2, width=500, height=500)

        view = artwork.summarise(album(big), cover_of(small), self._Answer(2000, 2000))

        row = view.archive_row
        assert row is not None
        assert (row.placeholder, row.meta) == ("not loaded", "2000×2000 · JPEG · 701 KB")

    def test_the_archive_having_nothing_is_its_own_placeholder(self) -> None:
        """A different word from "not loaded": there is nothing to load, rather
        than something that was not loaded (#433)."""
        view = artwork.summarise(album(art_of(1)), None, self._Answer(art=False))
        row = view.archive_row
        assert row is not None
        assert (row.placeholder, row.meta) == ("none", "no front cover for this release")

    def test_an_unmeasurable_archive_cover_says_so(self) -> None:
        view = artwork.summarise(album(art_of(1)), None, self._Answer())
        row = view.archive_row
        assert row is not None
        assert row.meta == "size could not be read"


class TestArchiveWins:
    """The archive as the third candidate, on the page (#276)."""

    def test_a_larger_archive_image_replaces_everything(self) -> None:
        mine = art_of(1, width=600, height=600)
        theirs = art_of(2, width=800, height=800)
        archive = art_of(3, width=1400, height=1400)

        view = artwork.summarise(album(mine, mine), cover_of(theirs), archive=archive)

        # Both the tracks and the folder cover are replaced, by the same image.
        assert {r.outcome for r in view.rows} == {artwork.Outcome.REPLACED}
        assert {r.written_from for r in view.rows} == {"the Cover Art Archive"}
        assert {r.written_image for r in view.rows} == {archive}

    def test_an_archive_image_that_loses_changes_nothing(self) -> None:
        big = art_of(1, width=3000, height=3000)

        view = artwork.summarise(
            album(big, big), cover_of(big), archive=art_of(2, width=400, height=400)
        )

        assert not any(r.writes for r in view.rows)

    def test_it_must_beat_the_best_the_album_has_not_just_the_cover(self) -> None:
        """An album whose tracks carry 3000px is not improved by a 2000px
        archive cover just because its folder file is smaller still."""
        tracks = art_of(1, width=3000, height=3000)
        folder = art_of(2, width=500, height=500)

        view = artwork.summarise(
            album(tracks), cover_of(folder), archive=art_of(3, width=2000, height=2000)
        )

        assert not any(r.written_from == "the Cover Art Archive" for r in view.rows)

    def test_per_track_artwork_is_still_never_overwritten(self) -> None:
        """The archive does not get to flatten a compilation, however large."""
        view = artwork.summarise(
            album(art_of(1, width=500, height=500), art_of(2, width=500, height=500)),
            cover_of(art_of(3, width=500, height=500)),
            archive=art_of(4, width=3000, height=3000),
        )

        assert not any(r.writes for r in view.rows)

    def test_it_wins_on_an_album_that_has_no_folder_cover(self) -> None:
        """The population #276 said the wins were in: art embedded in the files,
        no `cover.jpg` beside them, which is how an adopted library arrives.

        The archive needs no folder cover to be a candidate — it is an image
        from outside, and the folder file is not what carries it (#442).
        """
        mine = art_of(1, width=600, height=600)
        archive = art_of(2, width=1400, height=1400)

        view = artwork.summarise(album(mine, mine), None, archive=archive)

        assert [r.outcome for r in view.rows] == [artwork.Outcome.REPLACED]
        assert view.rows[0].written_from == "the Cover Art Archive"
        assert view.rows[0].written_image == archive
        assert view.rows[0].from_archive is True

    def test_it_must_still_beat_the_tracks_when_there_is_no_folder_cover(self) -> None:
        """With no cover file there is nothing else to compare against, so the
        tracks' own image is the incumbent — and ties go to what is there."""
        mine = art_of(1, width=1400, height=1400)

        same = artwork.summarise(
            album(mine, mine), None, archive=art_of(2, width=1400, height=1400)
        )
        smaller = artwork.summarise(
            album(mine, mine), None, archive=art_of(3, width=600, height=600)
        )

        assert not any(r.writes for r in same.rows)
        assert not any(r.writes for r in smaller.rows)

    def test_per_track_artwork_survives_having_no_folder_cover(self) -> None:
        """The promise, on the album shape #442 opened up.

        Two things hold it, and the second is the one doing the work here: the
        `not per_track` guard on `archive_wins` (which is what protects a
        compilation that HAS a folder cover), and — with no cover — the fact
        that an album with more than one image has no single `album_image`, so
        `album_best` cannot be measured and there is nothing for the archive to
        beat. Removing either leaves this passing; removing both does not, and
        the promise is worth pinning by its outcome rather than its mechanism.
        """
        view = artwork.summarise(
            album(art_of(1, width=500, height=500), art_of(2, width=500, height=500)),
            None,
            archive=art_of(3, width=3000, height=3000),
        )

        assert not any(r.writes for r in view.rows)


def test_a_track_row_replaced_by_the_cover_shows_the_incoming_image() -> None:
    """The gap this missed until #276: only the gap and folder rows carried an
    incoming image, so the commonest changing row of all — tracks about to be
    overwritten by a better folder cover — showed nothing on the right."""
    small = art_of(1, width=400, height=400)
    big = art_of(2, width=900, height=900)

    view = artwork.summarise(album(small, small), cover_of(big))

    tracks_row = view.rows[0]
    assert tracks_row.outcome is artwork.Outcome.REPLACED
    assert tracks_row.written_from == "cover.jpg"
    assert tracks_row.written_image == big


def test_only_the_archives_image_carries_the_musicbrainz_mark() -> None:
    """The hexagon is a claim about provenance. A folder cover may have come
    from a Bandcamp download, so it never gets one; the archive's image is the
    one case Harmonist can support (#276)."""
    small = art_of(1, width=400, height=400)
    folder = art_of(2, width=900, height=900)
    archive = art_of(3, width=1400, height=1400)

    from_cover = artwork.summarise(album(small), cover_of(folder))
    from_archive = artwork.summarise(album(small), cover_of(folder), archive=archive)

    assert from_cover.rows[0].written_image == folder
    assert from_cover.rows[0].from_archive is False
    assert from_archive.rows[0].written_image == archive
    assert from_archive.rows[0].from_archive is True


class TestSummaryWording:
    """The one line at the top of the page (#417), counted in files (#443).

    The distinct image is the right unit for the rows and the wrong one for this
    sentence: "1 image that could be better" understates a change to twelve
    files, and reads as a complaint about the picture rather than an offer.
    """

    def test_it_counts_the_tracks_that_change_not_the_images(self) -> None:
        small = art_of(1, width=300, height=300)
        view = artwork.summarise(album(small, small, small), cover_of(art_of(2)))

        assert view.summary == "Better artwork is available for 3 tracks."

    def test_the_folder_cover_is_named_when_it_is_all_that_changes(self) -> None:
        """The album's own art beats `cover.jpg`, so the FOLDER file catches up
        and no track moves (#410). A count of tracks would say zero here."""
        big = art_of(1, width=3000, height=3000)
        view = artwork.summarise(album(big, big), cover_of(art_of(2, width=500, height=500)))

        assert view.summary == "Better artwork is available for cover.jpg."

    def test_tracks_and_the_cover_together(self) -> None:
        """The archive beats both, so both change and both are named."""
        view = artwork.summarise(
            album(art_of(1, width=400, height=400), art_of(1, width=400, height=400)),
            cover_of(art_of(2, width=500, height=500)),
            archive=art_of(3, width=2000, height=2000),
        )

        assert view.summary == "Better artwork is available for 2 tracks and cover.jpg."

    def test_a_gap_and_an_improvement_are_two_clauses(self) -> None:
        small = art_of(1, width=300, height=300)
        view = artwork.summarise(album(small, small, None), cover_of(art_of(2)))

        assert view.summary == (
            "1 track is missing artwork, and better artwork is available for 2 tracks."
        )

    def test_a_gap_alone_says_only_that(self) -> None:
        same = art_of(1)
        view = artwork.summarise(album(same, None, None), cover_of(same))

        assert view.summary == "2 tracks are missing artwork."


class TestAnnouncingACoverThatWillBeCreated:
    """#457: a re-tag writes a `cover.jpg` into an album that hasn't got one —
    mandatory for Navidrome, and so not gated on anything the user pressed. The
    section had no way to say so, because the folder cover is modelled as a
    carrier of an image and a carrier that doesn't exist yet carries nothing."""

    Answer = TestCoverArtArchiveNote._Answer

    def test_an_album_with_a_cover_is_told_nothing(self) -> None:
        """`ensure_cover` returns an existing cover untouched — no rung runs, no
        file is written, and there is no event to announce."""
        one = art_of(1)
        assert artwork.summarise(album(one), cover_of(one)).creates_cover_from is None

    def test_the_archive_is_named_when_it_has_this_release(self) -> None:
        view = artwork.summarise(album(art_of(1)), None, self.Answer(art=True))
        assert view.creates_cover_from == "the Cover Art Archive"

    def test_it_falls_to_your_own_files_when_the_archive_has_nothing(self) -> None:
        """The second rung, and the one that needs no network. Naming the
        archive here would promise an image that has been established not to
        exist."""
        view = artwork.summarise(album(art_of(1)), None, self.Answer(art=False))
        assert view.creates_cover_from == "the artwork already in your files"

    def test_nothing_is_promised_when_nothing_could_make_a_cover(self) -> None:
        """No archive art and no embedded art: `ensure_cover` writes nothing, so
        the section says nothing rather than announcing a file that isn't
        coming."""
        view = artwork.summarise(album(None, None), None, self.Answer(art=False))
        assert view.creates_cover_from is None

    def test_an_unasked_archive_names_both_rungs_rather_than_guessing(self) -> None:
        """`caa is None` is "nobody has asked", not "there is nothing". Either
        rung may serve, and the wording carries that."""
        view = artwork.summarise(album(art_of(1)), None)
        assert view.creates_cover_from == (
            "the Cover Art Archive, or the artwork already in your files"
        )

    def test_an_unasked_archive_with_no_embedded_art_stays_conditional(self) -> None:
        view = artwork.summarise(album(None), None)
        assert view.creates_cover_from == "the Cover Art Archive, if it has this release"

    def test_an_unreadable_cover_is_not_a_missing_one(self) -> None:
        """The file IS there — `cached_cover` only checks that it exists, so
        `ensure_cover` hands it back without reading it and writes nothing. The
        view is built with `cover=None` and `cover_unreadable` set afterwards by
        `replace()`, so this case reaches the same code as a genuinely absent
        cover and has to be told apart from it (#112)."""
        from dataclasses import replace

        view = replace(artwork.summarise(album(art_of(1)), None), cover_unreadable=True)
        assert view.creates_cover_from is None

    def test_it_does_not_put_the_update_artwork_button_on_the_page(self) -> None:
        """The load-bearing one. `writes` is what draws that button, and
        `tagger.update_artwork` cannot create a folder cover — it only promotes
        over one that exists. Counting this as a write would offer a control
        that does not do what the sentence beside it just said."""
        view = artwork.summarise(album(art_of(1)), None, self.Answer(art=True))
        assert view.creates_cover_from  # there IS something to announce...
        assert view.writes is False  # ...and still no button
