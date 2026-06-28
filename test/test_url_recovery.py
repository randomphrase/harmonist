"""Tests for url_recovery — embedded Bandcamp URL extraction (no scraping)."""

from __future__ import annotations

import shutil
from pathlib import Path

from mutagen.mp4 import MP4

from harmonist.tagger import ATOM_COMMENT
from harmonist.url_recovery import extract_bandcamp_url, recover_store_url

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SINE_M4A = FIXTURES_DIR / "sine.m4a"


def _make_album(tmp_path: Path, *, comment: str | None = None) -> Path:
    d = tmp_path / "Artist" / "Album"
    d.mkdir(parents=True)
    f = d / "01 Track.m4a"
    shutil.copy(SINE_M4A, f)
    if comment is not None:
        audio = MP4(f)
        audio[ATOM_COMMENT] = [comment]
        audio.save()
    return d


# ---------- recover_store_url: any embedded Bandcamp URL ----------


def test_recovers_embedded_album_url(tmp_path):
    d = _make_album(tmp_path, comment="https://myartist.bandcamp.com/album/my-album")
    assert recover_store_url(d) == "https://myartist.bandcamp.com/album/my-album"


def test_recovers_embedded_album_url_behind_visit_prose(tmp_path):
    d = _make_album(tmp_path, comment="Visit https://myartist.bandcamp.com/album/my-album")
    assert recover_store_url(d) == "https://myartist.bandcamp.com/album/my-album"


def test_recovers_embedded_track_url(tmp_path):
    d = _make_album(tmp_path, comment="https://myartist.bandcamp.com/track/single")
    assert recover_store_url(d) == "https://myartist.bandcamp.com/track/single"


def test_recovers_artist_root_url(tmp_path):
    """A bare artist-root URL IS recovered (no scraping) — it's evidence the
    album is a Bandcamp purchase; the sync links it by title later."""
    d = _make_album(tmp_path, comment="Visit https://myartist.bandcamp.com")
    assert recover_store_url(d) == "https://myartist.bandcamp.com"


def test_non_bandcamp_url_recovers_nothing(tmp_path):
    d = _make_album(tmp_path, comment="https://example.com/album/x")
    assert recover_store_url(d) is None


def test_no_comment_recovers_nothing(tmp_path):
    d = _make_album(tmp_path)
    assert recover_store_url(d) is None


def test_no_audio_files_recovers_nothing(tmp_path):
    d = tmp_path / "Empty"
    d.mkdir()
    assert recover_store_url(d) is None


# ---------- extract_bandcamp_url: any Bandcamp URL (album or artist-root) ----------


def test_extract_returns_album_url():
    assert (
        extract_bandcamp_url("Visit https://x.bandcamp.com/album/y.")
        == "https://x.bandcamp.com/album/y"
    )


def test_extract_returns_artist_root():
    assert extract_bandcamp_url("Visit https://x.bandcamp.com") == "https://x.bandcamp.com"


def test_extract_returns_none_without_bandcamp():
    assert extract_bandcamp_url("https://example.com/x") is None
    assert extract_bandcamp_url("") is None
