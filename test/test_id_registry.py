"""Tests for path-derived album ids (#114).

The id for an album with no sidecar used to be a random UUID in a per-process
dict, so it changed on every restart and everything recorded against such an
album was orphaned. It is now a hash of the album's path relative to the library
root — stable by construction, with nothing persisted.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from harmonist import id_registry


@pytest.fixture(autouse=True)
def _no_root():
    """Each test sets its own root; don't leak one into the next."""
    id_registry.set_library_root(None)
    yield
    id_registry.set_library_root(None)


def test_the_same_directory_always_gets_the_same_id(tmp_path):
    id_registry.set_library_root(tmp_path)
    album = tmp_path / "Artist" / "Album"
    assert id_registry.get_or_mint(album) == id_registry.get_or_mint(album)


def test_the_id_survives_a_restart(tmp_path):
    """The point of #114. Re-importing the module is as complete a restart as a
    unit test can stage — it discards any module-level state, which is exactly
    what the old per-process dict relied on."""
    id_registry.set_library_root(tmp_path)
    album = tmp_path / "Artist" / "Album"
    before = id_registry.get_or_mint(album)

    reloaded = importlib.reload(id_registry)
    reloaded.set_library_root(tmp_path)

    assert reloaded.get_or_mint(album) == before


def test_different_albums_get_different_ids(tmp_path):
    id_registry.set_library_root(tmp_path)
    a = id_registry.get_or_mint(tmp_path / "Artist" / "One")
    b = id_registry.get_or_mint(tmp_path / "Artist" / "Two")
    assert a != b


def test_the_id_is_url_safe(tmp_path):
    """It goes in a URL path segment, and most routes carry a further segment
    after it — so it must contain no slashes and need no escaping."""
    id_registry.set_library_root(tmp_path)
    weird = tmp_path / "Ártist & Co." / "Album (Deluxe) #2 / Part 1"
    album_id = id_registry.get_or_mint(weird)
    assert album_id.isalnum() and album_id.isascii()


def test_the_id_is_relative_to_the_library_root(tmp_path):
    """A re-pointed bind-mount presents the same library at a different absolute
    path. Deriving from the absolute path would re-identify every album in it."""
    lib_a, lib_b = tmp_path / "mnt" / "music", tmp_path / "volume1" / "music"
    id_registry.set_library_root(lib_a)
    from_a = id_registry.get_or_mint(lib_a / "Artist" / "Album")
    id_registry.set_library_root(lib_b)
    from_b = id_registry.get_or_mint(lib_b / "Artist" / "Album")
    assert from_a == from_b


def test_a_path_outside_the_root_still_gets_an_id(tmp_path):
    """Shouldn't happen, but it must not raise — a stable string is all that's
    needed, so the absolute path is the honest fallback."""
    id_registry.set_library_root(tmp_path / "music")
    outside = Path("/elsewhere/Artist/Album")
    assert id_registry.get_or_mint(outside) == id_registry.get_or_mint(outside)


def test_path_for_finds_the_album_a_stale_url_refers_to(tmp_path):
    """A hash can't be inverted, so the reverse lookup re-derives over the
    scanned albums — what _find_album's race fallback needs."""
    id_registry.set_library_root(tmp_path)
    wanted = tmp_path / "Artist" / "Wanted"
    others = [tmp_path / "Artist" / "Other", wanted, tmp_path / "Artist" / "Third"]

    assert id_registry.path_for(id_registry.get_or_mint(wanted), others) == wanted
    assert id_registry.path_for("not-an-id", others) is None
