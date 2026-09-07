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

from harmonist import formats, images

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
