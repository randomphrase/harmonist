"""Shared pytest fixtures."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SINE_M4A = FIXTURES_DIR / "sine.m4a"


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
