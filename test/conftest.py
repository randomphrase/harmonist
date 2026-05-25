"""Shared pytest fixtures."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from harmonist import cover_art, mb_lookup, mb_search

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SINE_M4A = FIXTURES_DIR / "sine.m4a"

# Pristine references captured at import — conftest is imported before any test
# runs (and before any demo.install() can patch these), so these are the real
# implementations. Snapshotting at test-start instead is fragile: a leak from a
# prior test would be captured and faithfully "restored", perpetuating it.
_PRISTINE_GLOBALS = {
    (mb_lookup, "fetch_release"): mb_lookup.fetch_release,
    (mb_lookup, "fetch_release_urls"): mb_lookup.fetch_release_urls,
    (mb_lookup, "lookup_by_bandcamp_url"): mb_lookup.lookup_by_bandcamp_url,
    (mb_search, "search_releases"): mb_search.search_releases,
    (cover_art, "ensure_cover"): cover_art.ensure_cover,
}


@pytest.fixture(autouse=True)
def restore_module_globals():
    """`demo.install()` (any demo-mode app) monkey-patches mb_lookup /
    mb_search / cover_art at module level. Restore the pristine implementations
    after every test so a demo test can't leak its patches into the unit tests
    for those modules. Autouse + in conftest so it covers the whole suite
    (tests run in random order via pytest-randomly)."""
    yield
    for (module, attr), original in _PRISTINE_GLOBALS.items():
        setattr(module, attr, original)


@pytest.fixture(autouse=True)
def clear_activity():
    """The activity log is a process-level ring buffer; start each test with an
    empty feed so events don't leak between tests."""
    from harmonist import activity

    activity.clear()


@pytest.fixture
def album_with_tracks(tmp_path):
    """Factory: build an album dir with N copies of the sine fixture, named NN Title.m4a."""

    def _build(n: int, *, artist: str = "Test Artist", album: str = "Test Album") -> Path:
        album_dir = tmp_path / artist / album
        album_dir.mkdir(parents=True)
        for i in range(1, n + 1):
            target = album_dir / f"{i:02d} Track {i}.m4a"
            shutil.copy(SINE_M4A, target)
        return album_dir

    return _build
