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


def album(*art: EmbeddedArt | None, disc: int | None = None) -> list[tuple[str, TrackTags]]:
    """An album of `(file_name, tags)`, one entry per track, numbered from 1."""
    return [
        (f"{i:02d} Track.m4a", TrackTags(art=a, track_num=i, disc_num=disc))
        for i, a in enumerate(art, start=1)
    ]


class TestOutcomes:
    """The right-hand column: what a re-tag would do, per row."""

    def test_uniform_art_matching_the_cover_writes_nothing_new(self) -> None:
        one = art_of(1)
        view = artwork.summarise(album(one, one, one), cover_of(one))

        assert [r.outcome for r in view.rows] == [artwork.Outcome.SAME]
        assert view.writes is False

    def test_uniform_art_differing_from_the_cover_is_replaced(self) -> None:
        view = artwork.summarise(album(art_of(1), art_of(1)), cover_of(art_of(2)))

        assert [r.outcome for r in view.rows] == [artwork.Outcome.REPLACED]
        assert view.writes is True

    def test_per_track_art_is_kept(self) -> None:
        """The invariant this whole section rests on: a compilation's covers are
        preserved, so no row may promise a replacement."""
        view = artwork.summarise(album(art_of(1), art_of(2), art_of(3)), cover_of(art_of(9)))

        assert {r.outcome for r in view.rows} == {artwork.Outcome.KEPT}
        assert view.writes is False

    def test_gaps_are_filled_and_the_good_images_replaced(self) -> None:
        """#397 stated on screen: one distinct image plus two holes is not
        per-track art, so the two are filled by rewriting the two that were
        already right. The section must not soften this — it is what happens."""
        one = art_of(1)
        view = artwork.summarise(album(one, None, one, None), cover_of(art_of(2)))

        outcomes = {r.is_gap: r.outcome for r in view.rows}
        assert outcomes == {False: artwork.Outcome.REPLACED, True: artwork.Outcome.FILLED}

    def test_no_folder_cover_means_nothing_is_written(self) -> None:
        view = artwork.summarise(album(art_of(1), None), None)

        assert {r.outcome for r in view.rows} == {artwork.Outcome.KEPT}
        assert view.writes is False

    def test_outcomes_follow_the_taggers_own_verdict(self) -> None:
        """`preserves_per_track_art` in `tagger._prepare` and the KEPT rows here
        are the same decision. If they ever part company the page promises one
        thing and the button does another — so they ask one predicate."""
        digests = [art_of(1).digest, art_of(2).digest]
        assert artwork.has_per_track_art(digests) is True
        assert artwork.has_per_track_art([digests[0], digests[0], None]) is False


class TestVerdict:
    """The section's one line. Six shapes, none of them an alarm."""

    def test_settled(self) -> None:
        one = art_of(1)
        view = artwork.summarise(album(one, one), cover_of(one))
        assert view.verdict == "One image, on every track and in cover.jpg."

    def test_cover_disagrees(self) -> None:
        view = artwork.summarise(album(art_of(1), art_of(1)), cover_of(art_of(2)))
        assert view.verdict == "Your tracks carry a different image from the folder cover."

    def test_gaps_are_counted_not_alarmed_about(self) -> None:
        one = art_of(1)
        view = artwork.summarise(album(one, one, None), cover_of(one))
        assert view.verdict == "2 of 3 tracks carry artwork. 1 has none."

    def test_per_track_art_is_stated_as_a_fact(self) -> None:
        view = artwork.summarise(album(art_of(1), art_of(2)), cover_of(art_of(1)))
        assert view.verdict == "2 tracks, 2 different images."

    def test_folder_cover_only(self) -> None:
        view = artwork.summarise(album(None, None), cover_of(art_of(1)))
        assert view.verdict == "cover.jpg only. No track carries an embedded image."

    def test_nothing_at_all(self) -> None:
        view = artwork.summarise(album(None, None), None)
        assert view.verdict == (
            "No artwork. There is no cover file, and no track carries an embedded image."
        )


class TestRows:
    def test_one_row_per_distinct_image_naming_its_tracks(self) -> None:
        one, two = art_of(1), art_of(2)
        view = artwork.summarise(album(one, two, one), cover_of(one))

        assert len(view.images) == 2
        first, second = view.rows
        assert [t.name for t in first.tracks] == ["01 Track.m4a", "03 Track.m4a"]
        assert first.label(3) == "Tracks 1, 3"
        assert second.label(3) == "Track 2"

    def test_every_track_reads_as_all_of_them(self) -> None:
        one = art_of(1)
        assert artwork.summarise(album(one, one, one), cover_of(one)).rows[0].label(3) == (
            "All 3 tracks"
        )

    def test_consecutive_tracks_collapse_to_a_range(self) -> None:
        one, two = art_of(1), art_of(2)
        row = artwork.summarise(album(one, one, one, two), cover_of(one)).rows[0]
        assert row.label(4) == "Tracks 1–3"

    def test_the_gap_row_names_the_files(self) -> None:
        """The remedy is per file, so a count would not be enough to act on."""
        one = art_of(1)
        view = artwork.summarise(album(one, None), cover_of(one))
        gap = next(r for r in view.rows if r.is_gap)
        assert [t.name for t in gap.tracks] == ["02 Track.m4a"]

    def test_unreadable_files_vote_for_nothing(self) -> None:
        """A file Harmonist cannot open is not a file without artwork (#112)."""
        one = art_of(1)
        tracks = album(one, one)
        tracks.append(("03 Track.m4a", TrackTags(unreadable=True)))

        view = artwork.summarise(tracks, cover_of(one))

        assert view.unreadable == 1
        assert not any(r.is_gap for r in view.rows)
        # Not "All 3 tracks": the third was never read, so the row cannot claim it.
        assert view.rows[0].label(view.total_tracks) == "Tracks 1–2"


class TestDiscGrouping:
    def test_a_box_set_groups_by_disc(self) -> None:
        one, two = art_of(1), art_of(2)
        tracks = album(one, one, disc=1) + album(two, disc=2)

        discs = artwork.summarise(tracks, None).discs

        assert [d.number for d in discs] == [1, 2]
        assert [d.tracks for d in discs] == [2, 1]

    def test_a_single_disc_album_is_not_grouped(self) -> None:
        one = art_of(1)
        assert artwork.summarise(album(one, one, disc=1), None).discs == ()

    def test_an_image_spanning_two_discs_is_not_split_across_them(self) -> None:
        """Grouping is only drawn where it is true. One cover shared by two discs
        under two headings would report one image as two."""
        one = art_of(1)
        tracks = album(one, disc=1) + album(one, disc=2)

        assert artwork.summarise(tracks, None).discs == ()


class TestHeadingCount:
    def test_counts_the_images_the_tracks_carry(self) -> None:
        view = artwork.summarise(album(art_of(1), art_of(2)), cover_of(art_of(3)))
        assert view.count == "2 images"

    def test_counts_gaps_alongside(self) -> None:
        """ "1 image" over an album a third of whose tracks have none is true and
        misses the point."""
        one = art_of(1)
        view = artwork.summarise(album(one, None, one), cover_of(one))
        assert view.count == "1 image · 1 gap"

    def test_no_count_when_no_track_carries_an_image(self) -> None:
        assert artwork.summarise(album(None, None), cover_of(art_of(1))).count is None
