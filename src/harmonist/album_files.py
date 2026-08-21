"""Which audio files belong to one album — the single definition of that.

Nearly every album operation starts by asking "what are this album's tracks?".
Until #16 each answered it with the same one-liner over `album_dir.iterdir()`;
#16 added a directory-grouping rule on top, and #197 replaced that rule entirely.

An album is now **the files that name its MusicBrainz release**, wherever they
sit (`scanner.merge_by_identity`) — so which files those are is a property of
the ALBUM, not of any directory, and the answer comes from `Album.files`.

What is left here is the directory-scoped question, which is still the right one
in two places:

* **discovery** — reconcile and url_recovery meet a directory before anything
  knows what album it belongs to;
* **the scan itself**, which walks directories and only afterwards decides which
  of them go together.

So `audio_files(dir)` means exactly "the audio in this directory", with no
grouping cleverness. Nothing here reads a sidecar any more, which is also what
broke the import cycle in #200.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from . import formats


def audio_files(album_dir: Path) -> list[Path]:
    """The audio files IN this directory, in track order.

    Directory-scoped, deliberately: an album's own file list spans whatever
    directories its release's files occupy and belongs to the `Album` (#197).
    Use this when a directory is all you have.
    """
    return sorted((p for p in album_dir.iterdir() if formats.is_supported(p)), key=_key)


def video_files(album_dir: Path) -> list[Path]:
    """The video files in this directory, ordered like `audio_files`.

    Harmonist cannot tag these — that is #66 — so they are absent from
    `audio_files`, which is what the tagger, the matcher and the cover reader
    consume. But they are tracks the user has, carrying the same disc and
    position atoms as the audio, so the scan counts them towards completeness
    (#193): read, never written.
    """
    return sorted((p for p in album_dir.iterdir() if formats.is_video(p)), key=_key)


def for_paths(paths: Sequence[Path]) -> list[Path]:
    """Every audio file across an album's directories, in track order.

    The album-scoped question, answered from the folders `Album.paths` records.
    Ordered by directory first, so a two-disc album assembled from `CD1` and
    `CD2` still zips against the release's flattened tracklist in the right
    order — which is what a full tagging depends on.
    """
    seen: list[Path] = []
    for root in sorted(set(paths)):
        seen.extend(audio_files(root))
    return seen


def videos_for_paths(paths: Sequence[Path]) -> list[Path]:
    """Every video file across an album's directories, in track order.

    `for_paths` for the files the tagger must never see. Kept a separate list
    rather than folded into it: every caller of `for_paths` writes tags, and one
    that quietly gained video files would try to tag them (#66). The album page
    asks for both and keeps them apart the same way (#226).
    """
    seen: list[Path] = []
    for root in sorted(set(paths)):
        seen.extend(video_files(root))
    return seen


def _key(path: Path) -> tuple[tuple[object, ...], str]:
    return sort_key(Path(path.name))


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
