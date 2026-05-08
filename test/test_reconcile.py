"""Tests for reconcile.reconcile_album."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mutagen.mp4 import MP4

from harmonist import sidecar as sc
from harmonist.models import BandcampInfo, Sidecar
from harmonist.reconcile import reconcile_album, reconcile_pending


FIXTURES_DIR = Path(__file__).parent / "fixtures"
SINE_M4A = FIXTURES_DIR / "sine.m4a"


def _make_album(
    root: Path, *, mbid: str | None = None, comment: str | None = None
) -> Path:
    d = root / "Artist" / "Album"
    d.mkdir(parents=True)
    f = d / "01 Track.m4a"
    shutil.copy(SINE_M4A, f)
    if mbid or comment:
        audio = MP4(f)
        if mbid:
            audio["----:com.apple.iTunes:MusicBrainz Album Id"] = [mbid.encode("utf-8")]
        if comment:
            audio["\xa9cmt"] = [comment]
        audio.save()
    return d


def _no_urls(_mbid):
    return []


def _bandcamp_urls(*urls):
    return lambda _mbid: list(urls)


# ---------- skip cases ----------

def test_skips_album_with_existing_sidecar(tmp_path):
    album_dir = _make_album(tmp_path, mbid="rel-aaa", comment="Visit https://x.bandcamp.com")
    sc.write(album_dir, Sidecar(schema_version=1, source="manual", mb_release_id="pre-existing"))

    def boom(_mbid):
        raise AssertionError("should not query MB when sidecar already exists")

    result = reconcile_album(album_dir, fetch_urls=boom)
    assert result is None
    # Existing sidecar untouched
    loaded = sc.read(album_dir)
    assert loaded.mb_release_id == "pre-existing"


def test_skips_album_without_m4a_files(tmp_path):
    album_dir = tmp_path / "Empty"
    album_dir.mkdir()
    assert reconcile_album(album_dir, fetch_urls=_no_urls) is None


def test_skips_album_without_mbid_tag(tmp_path):
    album_dir = _make_album(tmp_path)  # no MBID
    assert reconcile_album(album_dir, fetch_urls=_no_urls) is None
    assert not sc.has_sidecar(album_dir)


# ---------- bandcamp source ----------

def test_writes_bandcamp_sidecar_when_comment_and_mb_match(tmp_path):
    album_dir = _make_album(
        tmp_path,
        mbid="rel-aaa",
        comment="Visit https://myartist.bandcamp.com",
    )
    fetch = _bandcamp_urls("https://myartist.bandcamp.com/album/my-album")
    result = reconcile_album(album_dir, fetch_urls=fetch)
    assert result is not None
    assert result.source == "bandcamp"
    assert result.bandcamp.url == "https://myartist.bandcamp.com/album/my-album"
    assert result.bandcamp.item_id is None  # unconfirmed until sync
    assert result.mb_release_id == "rel-aaa"
    assert result.tagged_at is not None

    loaded = sc.read(album_dir)
    assert loaded.source == "bandcamp"


def test_uses_canonical_mb_url_not_comment_url(tmp_path):
    """Sidecar records MB's URL even if ©cmt has a different (e.g. artist-page) URL."""
    album_dir = _make_album(
        tmp_path,
        mbid="rel-aaa",
        comment="Visit https://myartist.bandcamp.com",  # artist page
    )
    fetch = _bandcamp_urls("https://myartist.bandcamp.com/album/canonical-album")
    result = reconcile_album(album_dir, fetch_urls=fetch)
    assert result.bandcamp.url == "https://myartist.bandcamp.com/album/canonical-album"


def test_picks_first_bandcamp_url_when_mb_has_multiple(tmp_path):
    album_dir = _make_album(
        tmp_path, mbid="rel-aaa", comment="https://myartist.bandcamp.com"
    )
    fetch = _bandcamp_urls(
        "https://artist.notbandcamp.com/x",
        "https://primary.bandcamp.com/album/y",
        "https://other.bandcamp.com/album/z",
    )
    result = reconcile_album(album_dir, fetch_urls=fetch)
    assert result.bandcamp.url == "https://primary.bandcamp.com/album/y"


# ---------- manual source ----------

def test_writes_manual_when_no_bandcamp_comment(tmp_path):
    album_dir = _make_album(tmp_path, mbid="rel-aaa", comment="ripped from CD")
    fetch = _bandcamp_urls("https://x.bandcamp.com/album/y")  # MB knows of bandcamp
    result = reconcile_album(album_dir, fetch_urls=fetch)
    assert result is not None
    assert result.source == "manual"
    assert result.bandcamp is None
    assert result.mb_release_id == "rel-aaa"


def test_writes_manual_when_no_comment(tmp_path):
    album_dir = _make_album(tmp_path, mbid="rel-aaa")  # default sine.m4a, no ©cmt
    fetch = _bandcamp_urls("https://x.bandcamp.com/album/y")
    result = reconcile_album(album_dir, fetch_urls=fetch)
    assert result.source == "manual"


def test_writes_manual_when_mb_has_no_bandcamp_url(tmp_path):
    """User has Bandcamp ©cmt but MB doesn't actually link this release to Bandcamp."""
    album_dir = _make_album(
        tmp_path, mbid="rel-aaa", comment="https://x.bandcamp.com"
    )
    fetch = _bandcamp_urls("https://example.com/somewhere-else")  # no bandcamp URL
    result = reconcile_album(album_dir, fetch_urls=fetch)
    assert result.source == "manual"


def test_writes_manual_when_mb_has_no_url_relationships(tmp_path):
    album_dir = _make_album(
        tmp_path, mbid="rel-aaa", comment="https://x.bandcamp.com"
    )
    result = reconcile_album(album_dir, fetch_urls=_no_urls)
    assert result.source == "manual"


def test_writes_manual_when_mb_lookup_fails(tmp_path):
    """If MB lookup throws, fall back to manual rather than abort the whole scan."""
    album_dir = _make_album(
        tmp_path, mbid="rel-aaa", comment="https://x.bandcamp.com"
    )

    def explode(_mbid):
        raise RuntimeError("MB down")

    result = reconcile_album(album_dir, fetch_urls=explode)
    assert result.source == "manual"


# ---------- comment matching ----------

def test_matches_any_bandcamp_url_in_comment(tmp_path):
    """The comment doesn't have to be a clean URL — 'Visit X' counts."""
    album_dir = _make_album(
        tmp_path,
        mbid="rel-aaa",
        comment="Visit X at https://artist.bandcamp.com — brought to you by friends",
    )
    fetch = _bandcamp_urls("https://artist.bandcamp.com/album/y")
    result = reconcile_album(album_dir, fetch_urls=fetch)
    assert result.source == "bandcamp"


def test_case_insensitive_bandcamp_match(tmp_path):
    album_dir = _make_album(tmp_path, mbid="rel-aaa", comment="https://X.BANDCAMP.COM")
    fetch = _bandcamp_urls("https://x.bandcamp.com/album/y")
    result = reconcile_album(album_dir, fetch_urls=fetch)
    assert result.source == "bandcamp"


# ---------- batch ----------

def test_reconcile_pending_classifies_each_album(tmp_path):
    a = _make_album(tmp_path / "a", mbid="rel-1", comment="https://artist.bandcamp.com")
    b = tmp_path / "b" / "Artist" / "Album"
    b.mkdir(parents=True)
    shutil.copy(SINE_M4A, b / "01.m4a")
    audio = MP4(b / "01.m4a")
    audio["----:com.apple.iTunes:MusicBrainz Album Id"] = [b"rel-2"]
    audio.save()
    c = tmp_path / "c" / "Artist" / "Album"
    c.mkdir(parents=True)
    shutil.copy(SINE_M4A, c / "01.m4a")  # untagged

    fetch = _bandcamp_urls("https://artist.bandcamp.com/album/y")
    stats = reconcile_pending([a, b, c], fetch_urls=fetch)
    assert stats == {
        "reconciled_bandcamp": 1,
        "reconciled_manual": 1,
        "skipped": 1,
        "errors": 0,
    }
