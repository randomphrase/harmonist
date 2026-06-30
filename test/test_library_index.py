"""Tests for the in-memory sidecar/dedup index + its single update points."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from harmonist import library_index
from harmonist import sidecar as scmod
from harmonist.models import Album, BandcampInfo, Sidecar
from harmonist.sidecar import CURRENT_SCHEMA_VERSION


@dataclass
class _A:  # minimal Album stand-in: reset_from reads only .path + .sidecar
    path: Path
    sidecar: Sidecar | None


def _sc(store_url: str | None = None, item_id: int | None = None) -> Sidecar:
    return Sidecar(
        schema_version=CURRENT_SCHEMA_VERSION,
        store_url=store_url,
        mb_release_id="rel",
        bandcamp=BandcampInfo(item_id=item_id) if item_id is not None else None,
    )


def _albums(*items: tuple[Path, Sidecar]) -> list[Album]:
    return cast("list[Album]", [_A(p, s) for p, s in items])


def test_reset_from_builds_all_indexes():
    library_index.reset_from(
        _albums(
            (Path("/m/A"), _sc(store_url="https://x.bandcamp.com/album/a", item_id=111)),
            (Path("/m/B"), _sc(store_url="https://y.bandcamp.com/album/b")),  # unlinked
        )
    )
    assert library_index.item_ids() == {111}
    assert library_index.dir_for_url("https://x.bandcamp.com/album/a") == Path("/m/A")
    # slug match is subdomain-insensitive:
    assert library_index.slug_copies("https://other.bandcamp.com/album/a") == [(Path("/m/A"), True)]
    assert library_index.unlinked_slug_match("https://z.bandcamp.com/album/b") == Path("/m/B")


def test_upsert_then_remove_maintain_indexes():
    library_index.clear()
    library_index.upsert(Path("/m/A"), _sc(store_url="https://x.bandcamp.com/album/a", item_id=5))
    assert library_index.item_ids() == {5}
    # an item_id change must drop the stale id, not accumulate it:
    library_index.upsert(Path("/m/A"), _sc(store_url="https://x.bandcamp.com/album/a", item_id=6))
    assert library_index.item_ids() == {6}
    library_index.remove(Path("/m/A"))
    assert library_index.item_ids() == set()
    assert library_index.dir_for_url("https://x.bandcamp.com/album/a") is None


def test_slug_copies_carries_linked_flag_and_ambiguity():
    library_index.reset_from(
        _albums(
            (Path("/m/linked"), _sc(store_url="https://label.bandcamp.com/album/home", item_id=9)),
            (Path("/m/unl"), _sc(store_url="https://artist.bandcamp.com/album/home")),
        )
    )
    copies = dict(library_index.slug_copies("https://w.bandcamp.com/album/home"))
    assert copies == {Path("/m/linked"): True, Path("/m/unl"): False}
    # one unlinked copy → a single link target; the linked one isn't a candidate.
    assert library_index.unlinked_slug_match("https://w.bandcamp.com/album/home") == Path("/m/unl")


def test_sidecar_write_and_delete_are_the_single_update_points(tmp_path):
    library_index.clear()
    d = tmp_path / "Album"
    d.mkdir()
    scmod.write(d, _sc(store_url="https://x.bandcamp.com/album/foo", item_id=77))
    assert 77 in library_index.item_ids()  # write → indexed
    assert library_index.dir_for_url("https://x.bandcamp.com/album/foo") == d
    scmod.delete_all(tmp_path)
    assert library_index.item_ids() == set()  # delete_all → cleared
