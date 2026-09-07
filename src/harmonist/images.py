"""What an image is, read from its own first bytes.

Cover art arrives as opaque bytes from three different tag systems, and the
Artwork section (#155) states its dimensions — "1400×1400 · JPEG · 718 KB" is
most of what a user wants to know about an embedded image, and the difference
between a 600px cover and a 1400px one is the difference between a re-tag being
an improvement and a downgrade.

No image library. Pillow would be a new dependency in a project that has five,
and all this needs is the width and height a JPEG and a PNG both state in their
first few hundred bytes. Anything else — a WebP or an AVIF cover, which exist but
are vanishingly rare in embedded art — comes back None, and the section then
shows the format and size without the dimensions rather than guessing.

`digest` lives here too, so the one that `tagger` records, the one #131's store
keys its files by, and the one the album page compares are provably the same
function.
"""

from __future__ import annotations

import hashlib
import logging
from typing import NamedTuple

log = logging.getLogger(__name__)


def digest(data: bytes) -> str:
    """The content address of an image: sha256, hex.

    Load-bearing across three readers that must agree — `tagger._art_digests`
    decides whether per-track art is preserved, `artwork_store` names its files
    by this, and the album page compares tracks to each other with it. They were
    the same expression written three times before this existed.
    """
    return hashlib.sha256(data).hexdigest()


class Size(NamedTuple):
    width: int
    height: int


def dimensions(data: bytes) -> Size | None:
    """The image's pixel size, or None when it can't be read from these bytes.

    None is a real answer and not a failure: an unsupported format, a truncated
    image, a file that isn't an image at all. Every caller renders the rest of
    what it knows rather than inventing a size.
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return _png(data)
    if data[:2] == b"\xff\xd8":
        return _jpeg(data)
    return None


def _png(data: bytes) -> Size | None:
    """PNG states its size in the IHDR chunk, which the spec requires first."""
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    return Size(int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))


# The JPEG markers that introduce a frame header, whose payload carries the
# size. SOF0/1/2… run C0–CF with three exceptions that are NOT frame headers and
# would otherwise be read as one: DHT (C4), JPG (C8) and DAC (CC).
_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _jpeg(data: bytes) -> Size | None:
    """Walk the segment chain to the frame header.

    A JPEG's size sits behind however much EXIF and colour-profile metadata the
    encoder wrote, so there is no fixed offset to read — each segment states its
    own length and the walk follows them until a frame header turns up.

    Bounded by the data itself: a segment length that would step past the end,
    or a chain that stops making sense, returns None rather than looping. The
    bytes come off a user's disk and may be damaged, which is one of the things
    this section exists to reveal.
    """
    i = 2
    end = len(data)
    while i + 9 < end:
        if data[i] != 0xFF:  # not a marker where one must be: give up
            return None
        marker = data[i + 1]
        # Padding between segments is legal and encoders emit it.
        if marker == 0xFF:
            i += 1
            continue
        if marker in _SOF:
            return Size(
                int.from_bytes(data[i + 7 : i + 9], "big"),
                int.from_bytes(data[i + 5 : i + 7], "big"),
            )
        length = int.from_bytes(data[i + 2 : i + 4], "big")
        if length < 2:
            return None
        i += 2 + length
    return None
