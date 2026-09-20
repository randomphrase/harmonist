"""Derived tracklist review and page-local proposals; no I/O or stored decisions."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .formats import TagSet, TrackTags


@dataclass(frozen=True)
class Pairing:
    slots: tuple[int | None, ...]


def pairs(
    tags: Sequence[TrackTags],
    targets: Sequence[TagSet],
    eligible: set[int],
    *,
    propose: bool = False,
) -> Pairing:
    """Retain unique identities before considering only the remaining entries.

    A conflicting ID is never repaired by a number or singleton guess. Rippers
    use recording IDs (#538); retain those when they uniquely name a remaining
    track, while release-track IDs always take precedence.
    """
    slots: list[int | None] = [None] * len(tags)
    free = set(eligible)
    disk_ids = [t.owned.get("mb_release_track_id") for t in tags]
    mb_ids = [t.mb_release_track_id for t in targets]
    disk_counts, mb_counts = Counter(disk_ids), Counter(mb_ids)
    for i, ref in enumerate(disk_ids):
        if ref and disk_counts[ref] == mb_counts[ref] == 1:
            slot = mb_ids.index(ref)
            if slot in free:
                slots[i] = slot
                free.remove(slot)
    recordings = [
        t.owned.get("mb_track_id") if not disk_ids[i] else None for i, t in enumerate(tags)
    ]
    counts = Counter(recordings)
    recording_slots = {j for j in free if not mb_ids[j] or mb_counts[mb_ids[j]] == 1}
    for i, recording in enumerate(recordings):
        if not recording or counts[recording] != 1:
            continue
        candidates = [j for j in free & recording_slots if targets[j].mb_track_id == recording]
        if len(candidates) == 1:
            slots[i] = candidates[0]
            free.remove(candidates[0])
    # A repeated recording is common on CD rips. Exact numbering can identify
    # its occurrence, but only within tracks carrying that SAME recording ID.
    # This never turns an unknown/conflicting recording into a positional guess.
    single_disc = {targets[j].disc_num for j in eligible} == {1}
    occurrence = {
        i: (recording, tags[i].disc_num or (1 if single_disc else None), tags[i].track_num)
        for i, recording in enumerate(recordings)
        if recording and slots[i] is None
    }
    occurrence_counts = Counter(occurrence.values())
    for i, key in occurrence.items():
        candidates = [
            j
            for j in free & recording_slots
            if (targets[j].mb_track_id, targets[j].disc_num, targets[j].track_num) == key
        ]
        if None not in key and occurrence_counts[key] == 1 and len(candidates) == 1:
            slots[i] = candidates[0]
            free.remove(candidates[0])
    if propose:
        remaining = [
            i
            for i, t in enumerate(tags)
            if slots[i] is None
            and not disk_ids[i]
            and not t.owned.get("mb_track_id")
            and not t.unreadable
        ]
        # Repeated/missing MB IDs do not become valid identities via a proposal.
        invalid = {j for j in free if not mb_ids[j] or mb_counts[mb_ids[j]] != 1}
        free -= invalid
        numbers = {
            i: (tags[i].disc_num or (1 if single_disc else None), tags[i].track_num)
            for i in remaining
        }
        counts_by_number = Counter(numbers.values())
        for i in remaining:
            number = numbers[i]
            candidates = [j for j in free if (targets[j].disc_num, targets[j].track_num) == number]
            if None not in number and counts_by_number[number] == 1 and len(candidates) == 1:
                slots[i] = candidates[0]
                free.remove(candidates[0])
        remaining = [i for i in remaining if slots[i] is None]
        if not invalid and len(remaining) == len(free) == 1:
            i, j = remaining[0], next(iter(free))
            slots[i] = j
    return Pairing(tuple(slots))


@dataclass(frozen=True)
class Review:
    reasons: tuple[str, ...] = ()
    unassigned: int = 0

    @property
    def required(self) -> bool:
        return bool(self.reasons)


def review(
    tags: Sequence[TrackTags], targets: Sequence[TagSet], eligible: set[int], release_id: str
) -> Review:
    """Compare current structure with the identities, positions and totals on disk.

    File count alone is never evidence: an already-incomplete album stays
    incomplete without being a new structural change. Missing totals are also
    not an old structure we can invent. No previous cache payload is needed.
    """
    pairing = pairs(tags, targets, eligible)
    reasons: list[str] = []
    unassigned = 0
    for i, tag in enumerate(tags):
        if not tag.owned.get("mb_album_id"):
            continue
        slot = pairing.slots[i]
        if slot is None:
            # Preserve support for legacy payloads without release-track IDs.
            if (
                tag.owned.get("mb_release_track_id")
                or tag.owned.get("mb_track_id")
                or (
                    targets
                    and all(t.mb_release_track_id for t in targets)
                    and tag.owned.get("mb_album_id") == release_id
                )
            ):
                unassigned += 1
            continue
        target = targets[slot]
        for key, label in [("track_num", "Track numbering"), ("disc_num", "Disc numbering")]:
            before, after = tag.owned.get(key), getattr(target, key)
            if before is not None and before != after:
                reasons.append(f"{label} changed in MusicBrainz.")
        before = tag.owned.get("track_total")
        if before is not None and before != target.track_total:
            reasons.append(
                f"Disc {target.disc_num}: track count changed ({before} → {target.track_total})."
            )
        # An unsupported bonus medium alone must not demand an audio remap.
        before = tag.owned.get("disc_total")
        if len(eligible) == len(targets) and before is not None and before != target.disc_total:
            reasons.append(f"Disc count changed ({before} → {target.disc_total}).")
    if unassigned:
        reasons.append(
            f"{unassigned} {'file needs' if unassigned == 1 else 'files need'} track assignments."
        )
    return Review(tuple(dict.fromkeys(reasons)), unassigned)
