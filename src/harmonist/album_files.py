"""Which audio files belong to one album — the single definition of that.

Nearly every album operation starts by asking "what are this album's tracks?",
and until #16 each of them answered it independently with the same one-liner:
``sorted(p for p in album_dir.iterdir() if formats.is_supported(p))``. That
answer is right for the flat layout Harmonist creates, and wrong for a release
the user split across per-disc subdirectories, which is a shape a decades-old
adopted library is full of.

So the rule lives here once, and the call sites ask rather than re-deriving:

    a directory that DECLARES itself an album (it has a sidecar) but holds no
    audio of its own owns every audio file beneath it.

Nothing else about the library changes: no file moves, no renames, no migration.
The declaration is the sidecar the user (or `reconcile.promote_split_release`)
puts in the parent directory, and removing that sidecar is what undoes it —
which is the escape hatch, since the per-disc directories then answer for
themselves again exactly as before.

Deliberately narrow. A directory with audio of its own is an album, full stop,
even when it also has audio-bearing subdirectories — so no album that exists
today can change shape under this rule. Harmonist has never written a sidecar
into a directory containing no audio (`reconcile_album` returns early, and
tagging is only ever aimed at an album path), so the grouped shape is
unreachable by accident: it only arises where something deliberately created it.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import formats
from .models import SIDECAR_FILENAME


def audio_files(album_dir: Path) -> list[Path]:
    """This album's audio files, in track order.

    The directory's own files when it has any (the flat case, which is every
    album Harmonist creates), else — when the directory declares itself an
    album with a sidecar — every audio file beneath it, disc directory by disc
    directory. Returns [] for a directory that is not an album at all.
    """
    own = sorted(p for p in album_dir.iterdir() if formats.is_supported(p))
    if own or not (album_dir / SIDECAR_FILENAME).exists():
        return own
    return descendant_audio_files(album_dir)


def descendant_audio_files(album_dir: Path) -> list[Path]:
    """Every audio file below `album_dir`, ordered by subdirectory then name.

    Split out from `audio_files` because the scanner's walk has already
    established that the directory holds no audio of its own and wants only
    this half — and because `reconcile` needs it to evaluate a candidate
    parent that has no sidecar yet.
    """
    found = [p for p in album_dir.rglob("*") if formats.is_supported(p) and p.is_file()]
    return sorted(found, key=lambda p: sort_key(p.relative_to(album_dir)))


def rel_name(album_dir: Path, path: Path) -> str:
    """How a record names one of this album's files.

    The path relative to the album directory — which for a flat album is the
    bare filename every record has always carried, and for a split release is
    "CD2/01 - Intro.m4a" rather than a "01 - Intro.m4a" that two discs both
    answer to. Records key reverts by this name (`tag_history`), so a collision
    there does not merely display wrong: it restores one disc's tags onto the
    other's file.

    Falls back to the bare name if `path` is somehow not under `album_dir`,
    since a name that is merely ambiguous beats raising from inside an audit
    write (see the `error-handling` skill).
    """
    try:
        return str(path.relative_to(album_dir))
    except ValueError:
        return path.name


def sort_key(rel: Path) -> tuple[tuple[object, ...], str]:
    """Order one album's files by (directory, filename).

    The directory half is compared NATURALLY — "CD2" before "CD10", which plain
    string order gets backwards. That matters more than it looks: full tagging
    zips this list against the release's flattened track list positionally, so a
    ten-disc box set ordered lexically would tag every disc as the wrong one.

    The filename half stays a plain string compare, byte-identical to the
    `sorted(...)` every call site used before this module existed. Flat albums
    have no directory component at all, so their order is unchanged — this
    function cannot reorder an album that isn't split.
    """
    return (tuple(_natural(part) for part in rel.parts[:-1]), rel.name)


def _natural(text: str) -> tuple[object, ...]:
    """Split a name into (text, number, text, …) so digit runs compare as
    numbers. Segments are tagged with a type flag before comparison, because
    tuple comparison raises on `str` vs `int` and a library is guaranteed to
    contain both "CD1" and "Bonus".
    """
    return tuple(
        (1, int(chunk), "") if chunk.isdigit() else (0, 0, chunk.lower())
        for chunk in re.split(r"(\d+)", text)
        if chunk
    )
