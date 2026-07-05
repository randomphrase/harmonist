"""Tests for reconcile.store_url_for_tagging — the tag-time derivation of a
Bandcamp store_url so a manually-assigned download reaches Needs Link.

Sources in preference order: embedded ©cmt /album/ URL → MB url-rel → artist-root
placeholder; all gated by ©cmt Bandcamp evidence."""

from __future__ import annotations

import shutil
from pathlib import Path

from mutagen.mp4 import MP4

from harmonist.reconcile import store_url_for_tagging
from harmonist.tagger import ATOM_COMMENT

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


def _no_urls(_mbid):
    return []


def _mb_urls(*urls):
    return lambda _mbid: list(urls)


def test_prefers_embedded_album_url(tmp_path):
    """A precise /album/ URL in the comment wins — no MB lookup needed."""
    d = _make_album(tmp_path, comment="https://artist.bandcamp.com/album/real")

    def boom(_mbid):
        raise AssertionError("MB should not be queried when a precise URL is embedded")

    assert (
        store_url_for_tagging(d, "rel-1", fetch_urls=boom)
        == "https://artist.bandcamp.com/album/real"
    )


def test_falls_back_to_mb_url_when_comment_has_only_root(tmp_path):
    """Artist-root comment + MB has a canonical Bandcamp URL → use MB's."""
    d = _make_album(tmp_path, comment="Visit https://artist.bandcamp.com")
    fetch = _mb_urls("https://other.com/x", "https://artist.bandcamp.com/album/canonical")
    assert (
        store_url_for_tagging(d, "rel-1", fetch_urls=fetch)
        == "https://artist.bandcamp.com/album/canonical"
    )


def test_falls_back_to_artist_root_when_mb_has_no_bandcamp(tmp_path):
    """Artist-root comment + MB has no Bandcamp URL → keep the artist-root as a
    placeholder so the album is still recognised as a Bandcamp purchase."""
    d = _make_album(tmp_path, comment="Visit https://artist.bandcamp.com")
    assert store_url_for_tagging(d, "rel-1", fetch_urls=_no_urls) == "https://artist.bandcamp.com"


def test_none_when_no_bandcamp_evidence(tmp_path):
    """No Bandcamp URL in the comment at all → None (a CD rip stays Complete)."""
    d = _make_album(tmp_path, comment="ripped from CD")
    assert (
        store_url_for_tagging(d, "rel-1", fetch_urls=_mb_urls("https://x.bandcamp.com/album/y"))
        is None
    )


def test_none_when_no_comment(tmp_path):
    d = _make_album(tmp_path)
    assert store_url_for_tagging(d, "rel-1", fetch_urls=_no_urls) is None


def test_logs_the_comment_when_present_but_not_bandcamp(tmp_path, caplog):
    """Observability: a non-empty comment with no bandcamp.com URL (e.g. the "36"
    albums' "Visit https://3six.net") is logged, so a genuinely-purchased album
    silently landing in Library is explainable from the logs."""
    import logging

    d = _make_album(tmp_path, comment="Visit https://3six.net")
    with caplog.at_level(logging.INFO, logger="harmonist.reconcile"):
        assert store_url_for_tagging(d, "rel-1", fetch_urls=_no_urls) is None
    assert any("3six.net" in r.getMessage() for r in caplog.records)


def test_no_log_when_comment_empty(tmp_path, caplog):
    """A plain CD rip (no comment) isn't logged — only the surprising 'has a
    comment but no bandcamp URL' case is worth the line."""
    import logging

    d = _make_album(tmp_path)  # no comment
    with caplog.at_level(logging.INFO, logger="harmonist.reconcile"):
        assert store_url_for_tagging(d, "rel-1", fetch_urls=_no_urls) is None
    assert not any("no Bandcamp store URL" in r.getMessage() for r in caplog.records)


def test_none_when_no_audio_files(tmp_path):
    d = tmp_path / "Empty"
    d.mkdir()
    assert store_url_for_tagging(d, "rel-1", fetch_urls=_no_urls) is None
