"""Tests for the in-memory potential-download store."""

from __future__ import annotations

from harmonist import pending_downloads
from harmonist.pending_downloads import PendingPurchase


def _p(item_id: int, band: str = "B", title: str = "T") -> PendingPurchase:
    return PendingPurchase(item_id=item_id, band=band, title=title, url="u", fmt="alac")


def test_replace_all_is_atomic_swap_and_dedups_by_item_id():
    pending_downloads.replace_all([_p(1), _p(2), _p(1, band="dup")])
    assert pending_downloads.count() == 2  # item_id 1 collapsed
    # A second swap fully replaces (not appends).
    pending_downloads.replace_all([_p(3)])
    assert [p.item_id for p in pending_downloads.all_pending()] == [3]


def test_all_pending_sorted_by_band_then_title():
    pending_downloads.replace_all(
        [_p(1, band="Zed", title="A"), _p(2, band="Abe", title="Z"), _p(3, band="Abe", title="A")]
    )
    assert [p.item_id for p in pending_downloads.all_pending()] == [3, 2, 1]


def test_remove_and_get():
    pending_downloads.replace_all([_p(7), _p(8)])
    assert pending_downloads.get(7) is not None
    pending_downloads.remove(7)
    assert pending_downloads.get(7) is None
    assert pending_downloads.count() == 1
    pending_downloads.remove(7)  # idempotent — no raise
    pending_downloads.replace_all([])  # leave clean for other tests
