"""Page-local track pairing. Nothing here persists a decision or writes tags."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import compare, formats, match, mb_lookup, tagger, track_structure
from .models import Release


class AssignmentChanged(ValueError):
    """The files are no longer the ones the user reviewed."""


@dataclass(frozen=True)
class Entry:
    title: str
    number: str
    length: int | None
    filename: str = ""
    path: str = ""


def _number(disc: int | None, track: int | None) -> str:
    return f"{disc or '?'}.{track or '?'}"


def _order(value: str, count: int, rows: int) -> list[int | None]:
    parts = value.split(",")
    if len(parts) != rows or any(p != "-" and not p.isascii() for p in parts):
        raise ValueError("invalid assignment order")
    if any(p != "-" and not p.isdecimal() for p in parts):
        raise ValueError("invalid assignment order")
    slots = [None if p == "-" else int(p) for p in parts]
    if sorted(p for p in slots if p is not None) != list(range(count)):
        raise ValueError("invalid assignment: each entry must occur exactly once")
    return slots


def encode(order: list[int | None]) -> str:
    return ",".join("-" if i is None else str(i) for i in order)


@dataclass(frozen=True)
class Draft:
    disk_order: str
    mb_order: str
    disk_fingerprint: str


@dataclass
class Panel:
    files: list[Path]
    disk: list[Entry]
    mb: list[Entry]
    disk_order: list[int | None]
    mb_order: list[int | None]
    disk_fingerprint: str
    known_slots: tuple[int | None, ...] | None = None

    @property
    def proposed(self) -> frozenset[tuple[int, int]]:
        if self.known_slots is None:
            return frozenset()
        return frozenset(
            (d, m)
            for d, m in zip(self.disk_order, self.mb_order, strict=True)
            if d is not None and m is not None and self.known_slots[d] != m
        )

    @property
    def draft(self) -> Draft:
        return Draft(encode(self.disk_order), encode(self.mb_order), self.disk_fingerprint)

    @property
    def rows(self) -> list[tuple[Entry | None, Entry | None]]:
        return [
            (self.disk[d] if d is not None else None, self.mb[m] if m is not None else None)
            for d, m in zip(self.disk_order, self.mb_order, strict=True)
        ]

    @property
    def paired(self) -> bool:
        return all(d is None or m is not None for d, m in zip(self.disk_order, self.mb_order))

    def mapping(self) -> dict[Path, int]:
        return {
            self.files[d]: m
            for d, m in zip(self.disk_order, self.mb_order, strict=True)
            if d is not None and m is not None
        }

    def move(self, move: str) -> None:
        if move == "release-only":
            self.disk_order = list(range(len(self.disk))) + [None] * len(self.mb)
            self.mb_order = [None] * len(self.disk) + list(range(len(self.mb)))
            return
        if move == "add-gap":
            if len(self.disk_order) >= len(self.disk) + len(self.mb) + 1:
                raise ValueError("Every track already has room for a gap")
            self.disk_order.append(None)
            self.mb_order.append(None)
            return
        side, index, direction = move.split(":")
        if side not in {"disk", "mb"} or direction not in {"up", "down"}:
            raise ValueError("invalid assignment move")
        order = self.disk_order if side == "disk" else self.mb_order
        i = int(index)
        j = i + (-1 if direction == "up" else 1)
        if not (0 <= i < len(order) and 0 <= j < len(order)):
            raise ValueError("invalid assignment move")
        order[i], order[j] = order[j], order[i]


def panel(
    files: list[Path], release: Release, draft: Draft | None = None, *, confirmed: bool = False
) -> Panel:
    tags = [formats.read_tags(f) for f in files]
    if any(t.unreadable for t in tags):
        raise OSError("A file could not be read. Repair it before editing assignments.")
    stats = [f.stat() for f in files]
    fingerprint = hashlib.sha256(
        json.dumps(
            [
                [str(f), s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, t.owned]
                for f, s, t in zip(files, stats, tags, strict=True)
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    if draft is not None and draft.disk_fingerprint != fingerprint:
        raise AssignmentChanged("Files changed since the review. Review the assignments again.")
    # No transforms (#544): this panel reads the tagsets for TRACK titles and
    # ids, to line files up against the tracklist. Every transform so far is
    # album-scoped, and a user's album-title spelling has no bearing on which
    # file is which track.
    mb_tags = tagger.tagsets_for(release, frozenset())
    lengths = match.mb_track_lengths(release)
    root = Path(os.path.commonpath([f.parent for f in files])) if files else Path(".")
    disk = [
        Entry(
            t.title or f.name,
            _number(t.disc_num, t.track_num),
            t.duration_ms,
            f.name,
            str(f.relative_to(root)),
        )
        for f, t in zip(files, tags, strict=True)
    ]
    mb = [
        Entry(t.title, _number(t.disc_num, t.track_num), length)
        for t, length in zip(mb_tags, lengths, strict=True)
    ]
    video = set(mb_lookup.video_media_of(release))
    eligible = {i for i, t in enumerate(mb_tags) if t.disc_num not in video}
    known = track_structure.pairs(tags, mb_tags, eligible)
    if draft is None:
        slots = compare.assign(
            [compare.identity_of(t) for t in tags],
            [compare.TrackIdentity.of_tagset(t) for t in mb_tags],
        )
        if confirmed:
            proposal = track_structure.pairs(tags, mb_tags, eligible, propose=True)
            slots = list(proposal.slots)
        disk_order: list[int | None] = [None] * len(mb)
        for i, slot in enumerate(slots):
            if slot is not None:
                disk_order[slot] = i
            else:
                disk_order.append(i)
        mb_order: list[int | None] = list(range(len(mb))) + [None] * (len(disk_order) - len(mb))
    else:
        rows = len(draft.disk_order.split(","))
        if not max(len(disk), len(mb)) <= rows <= len(disk) + len(mb) + 1:
            raise ValueError("invalid assignment row count")
        disk_order = _order(draft.disk_order, len(disk), rows)
        mb_order = _order(draft.mb_order, len(mb), rows)
    return Panel(
        files, disk, mb, disk_order, mb_order, fingerprint, known.slots if confirmed else None
    )
