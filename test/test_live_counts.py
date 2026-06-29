"""Tests for live_counts — the single source of truth for inbox/library counts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from harmonist import live_counts
from harmonist.models import Album, AlbumState


@dataclass
class _A:  # minimal stand-in for Album (reset_from only reads .state)
    state: AlbumState


def _albums(*states: AlbumState) -> list[Album]:
    return cast("list[Album]", [_A(s) for s in states])


def test_reset_from_counts_by_state():
    live_counts.reset_from(
        _albums(
            AlbumState.NEW,
            AlbumState.NEEDS_SYNC,
            AlbumState.NEEDS_SYNC,
            AlbumState.COMPLETE,
            AlbumState.INCOMPLETE,
        )
    )
    s = live_counts.to_status()
    assert s["needs_sync"] == 2
    assert s["new"] == 1
    assert s["library"] == 2  # COMPLETE + INCOMPLETE
    assert s["inbox"] == 3  # NEW + 2x NEEDS_SYNC (non-terminal)


def test_move_adjusts_between_scans():
    live_counts.reset_from(_albums(AlbumState.NEEDS_SYNC, AlbumState.NEEDS_SYNC))
    live_counts.move(AlbumState.NEEDS_SYNC, AlbumState.COMPLETE)  # one links
    s = live_counts.to_status()
    assert s["needs_sync"] == 1
    assert s["library"] == 1
    assert s["inbox"] == 1


def test_move_floors_at_zero_and_noops_on_same():
    live_counts.reset_from(_albums(AlbumState.NEW))
    live_counts.move(AlbumState.NEW, AlbumState.NEW)  # no-op
    live_counts.move(AlbumState.NEEDS_SYNC, AlbumState.COMPLETE)  # old already 0 → floor
    s = live_counts.to_status()
    assert s["new"] == 1
    assert s["needs_sync"] == 0  # floored, not negative
    assert s["complete"] == 1
