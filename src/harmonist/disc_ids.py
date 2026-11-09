"""A MusicBrainz disc ID from a CD table of contents in the tags (#984).

No network or persistence. The answer lists search results and nothing else:
XLD also synthesizes `iTunes_CDDB_1` on a transcode, so the tag can't vouch for
a physical disc.
"""

import base64
import hashlib
from collections import defaultdict
from collections.abc import Sequence
from itertools import pairwise
from typing import NamedTuple

from .formats.types import ScanFields

_MAX_TRACKS = 99
_MUSICBRAINZ_BASE64 = bytes.maketrans(b"+/=", b"._-")


class Toc(NamedTuple):
    lead_out: int
    offsets: tuple[int, ...]


def parse(value: str) -> Toc | None:
    """`iTunes_CDDB_1`: `cddb_id+lead_out+count+offset+…`, in decimal sectors."""
    cddb_id, *numbers = value.strip().split("+")
    if not cddb_id or len(numbers) < 3 or not all(n.isascii() and n.isdigit() for n in numbers):
        return None
    lead_out, count, *offsets = map(int, numbers)
    if not 1 <= count <= _MAX_TRACKS or len(offsets) != count:
        return None
    if any(later <= earlier for earlier, later in pairwise((*offsets, lead_out))):
        return None
    return Toc(lead_out, tuple(offsets))


def disc_id(toc: Toc) -> str:
    """SHA-1 of the first and last track numbers, the lead-out and 99 offsets."""
    padding = (0,) * (_MAX_TRACKS - len(toc.offsets))
    text = f"{1:02X}{len(toc.offsets):02X}{toc.lead_out:08X}" + "".join(
        f"{offset:08X}" for offset in (*toc.offsets, *padding)
    )
    digest = hashlib.sha1(text.encode("ascii"), usedforsecurity=False).digest()
    return base64.b64encode(digest).translate(_MUSICBRAINZ_BASE64).decode("ascii")


def evidence(fields: Sequence[ScanFields]) -> str | None:
    """The first disc's ID, when every disc of the album is a whole one.

    One bad disc rules out the album, as one bad file rules out a barcode: tags
    that contradict the files are no basis for a search. Only the first disc is
    looked up, since that finds the release and the review covers the rest.
    """
    if not fields or any(f.unreadable for f in fields):
        return None
    discs: dict[int, list[ScanFields]] = defaultdict(list)
    for f in fields:
        discs[f.disc_num or 1].append(f)
    first: Toc | None = None
    for number in sorted(discs):
        toc = _whole_disc(discs[number])
        if toc is None:
            return None
        first = first or toc
    return disc_id(first) if first else None


def _whole_disc(tracks: Sequence[ScanFields]) -> Toc | None:
    """The table of contents every file agrees on, with one file per track."""
    values = {f.cd_toc for f in tracks}
    value = values.pop() if len(values) == 1 else None
    toc = parse(value) if value else None
    return toc if toc and len(toc.offsets) == len(tracks) else None
