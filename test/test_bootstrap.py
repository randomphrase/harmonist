"""Tests for bootstrap — Phase A (always) + Phase B (with creds)."""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
from mutagen.mp4 import MP4

from harmonist import bootstrap, sidecar as sc
from harmonist.bootstrap import (
    BootstrapResult,
    bootstrap as bootstrap_fn,
    derive_sidecars_from_tags,
    reconcile_with_bandcamp_purchases,
)
from harmonist.models import BandcampInfo, Sidecar


FIXTURES_DIR = Path(__file__).parent / "fixtures"
SINE_M4A = FIXTURES_DIR / "sine.m4a"


def _make_album(root: Path, *, artist: str, album: str, mbid: str | None = None) -> Path:
    d = root / artist / album
    d.mkdir(parents=True)
    f = d / "01 Track.m4a"
    shutil.copy(SINE_M4A, f)
    if mbid:
        audio = MP4(f)
        audio["----:com.apple.iTunes:MusicBrainz Album Id"] = [mbid.encode("utf-8")]
        audio.save()
    return d


class _StubItem:
    def __init__(self, item_id, band_name, item_title, **url_hints):
        self._data = {
            "item_id": item_id,
            "band_name": band_name,
            "item_title": item_title,
            "url_hints": url_hints,
            "band_id": 999,
        }
        self.item_id = item_id
        self.band_name = band_name
        self.item_title = item_title


class _StubBandcamp:
    def __init__(self, purchases):
        self.purchases = purchases


# ============================================================================
# Phase A — derive sidecars from tags
# ============================================================================


def test_phase_a_writes_sidecar_for_tagged_album(tmp_path):
    album_dir = _make_album(tmp_path, artist="Artist", album="Album", mbid="rel-aaa")
    stats = derive_sidecars_from_tags(tmp_path)
    assert stats.derived == 1
    assert stats.skipped_existing == 0
    assert stats.skipped_untagged == 0
    loaded = sc.read(album_dir)
    assert loaded is not None
    assert loaded.source == "manual"
    assert loaded.mb_release_id == "rel-aaa"
    assert loaded.tagged_at is not None


def test_phase_a_skips_existing_sidecar(tmp_path):
    album_dir = _make_album(tmp_path, artist="Artist", album="Album", mbid="rel-aaa")
    sc.write(
        album_dir,
        Sidecar(schema_version=1, source="manual", mb_release_id="pre-existing"),
    )
    stats = derive_sidecars_from_tags(tmp_path)
    assert stats.derived == 0
    assert stats.skipped_existing == 1
    # And the sidecar is unchanged
    loaded = sc.read(album_dir)
    assert loaded.mb_release_id == "pre-existing"


def test_phase_a_skips_untagged_album(tmp_path):
    _make_album(tmp_path, artist="Artist", album="Album")  # no MBID
    stats = derive_sidecars_from_tags(tmp_path)
    assert stats.derived == 0
    assert stats.skipped_untagged == 1


def test_phase_a_dry_run_does_not_write(tmp_path):
    album_dir = _make_album(tmp_path, artist="Artist", album="Album", mbid="rel-aaa")
    stats = derive_sidecars_from_tags(tmp_path, dry_run=True)
    assert stats.derived == 1
    assert not sc.has_sidecar(album_dir)


def test_phase_a_handles_mixed_library(tmp_path):
    _make_album(tmp_path, artist="A1", album="Tagged", mbid="rel-1")
    _make_album(tmp_path, artist="A2", album="Untagged")
    pre_dir = _make_album(tmp_path, artist="A3", album="HasSidecar", mbid="rel-3")
    sc.write(pre_dir, Sidecar(schema_version=1, source="bandcamp",
                              bandcamp=BandcampInfo(url="https://x.bandcamp.com/album/y", item_id=1)))

    stats = derive_sidecars_from_tags(tmp_path)
    assert stats.derived == 1
    assert stats.skipped_untagged == 1
    assert stats.skipped_existing == 1


def test_phase_a_idempotent(tmp_path):
    _make_album(tmp_path, artist="Artist", album="Album", mbid="rel-aaa")
    derive_sidecars_from_tags(tmp_path)
    stats = derive_sidecars_from_tags(tmp_path)  # second run
    assert stats.derived == 0
    assert stats.skipped_existing == 1


# ============================================================================
# Phase B — reconcile Bandcamp purchases with on-disk MBIDs
# ============================================================================


def test_phase_b_upgrades_matched_album_and_appends_ignore(tmp_path):
    # On-disk: one tagged album with a manual sidecar
    album_dir = _make_album(tmp_path, artist="Artist", album="Album", mbid="rel-matched")
    sc.write(album_dir, Sidecar(schema_version=1, source="manual", mb_release_id="rel-matched"))

    cookies_path = tmp_path / "cookies.txt"
    cookies_path.write_text("dummy")
    ignores_path = tmp_path / "ignores.txt"

    purchases = [
        _StubItem(item_id=12345, band_name="Artist", item_title="Album",
                  subdomain="artist", slug="album", item_type="album"),
    ]
    factory = lambda _path: _StubBandcamp(purchases)
    lookup = lambda url: "rel-matched" if url == "https://artist.bandcamp.com/album/album" else None

    stats = reconcile_with_bandcamp_purchases(
        tmp_path, cookies_path, ignores_path,
        bandcamp_factory=factory, lookup_fn=lookup,
    )
    assert stats.matched == 1
    assert stats.unmatched_purchases == 0

    # Sidecar now upgraded
    loaded = sc.read(album_dir)
    assert loaded.source == "bandcamp"
    assert loaded.bandcamp.url == "https://artist.bandcamp.com/album/album"
    assert loaded.bandcamp.item_id == 12345
    assert loaded.mb_release_id == "rel-matched"  # unchanged

    # Ignores file populated
    assert "12345" in ignores_path.read_text()
    assert "Artist / Album" in ignores_path.read_text()


def test_phase_b_skips_purchase_without_matching_mbid(tmp_path):
    album_dir = _make_album(tmp_path, artist="A", album="B", mbid="rel-on-disk")
    sc.write(album_dir, Sidecar(schema_version=1, source="manual", mb_release_id="rel-on-disk"))

    cookies_path = tmp_path / "cookies.txt"
    cookies_path.write_text("d")
    ignores_path = tmp_path / "ignores.txt"

    purchases = [
        _StubItem(item_id=1, band_name="X", item_title="Y",
                  subdomain="x", slug="y", item_type="album"),
    ]
    # MB lookup returns a different MBID — purchase doesn't match anything on disk
    lookup = lambda url: "rel-different"

    stats = reconcile_with_bandcamp_purchases(
        tmp_path, cookies_path, ignores_path,
        bandcamp_factory=lambda _: _StubBandcamp(purchases), lookup_fn=lookup,
    )
    assert stats.matched == 0
    assert stats.unmatched_purchases == 1
    assert not ignores_path.exists() or "1  #" not in ignores_path.read_text()


def test_phase_b_skips_purchase_with_no_mb_match(tmp_path):
    """MB lookup returns None — purchase has no MB release linked."""
    cookies_path = tmp_path / "cookies.txt"
    cookies_path.write_text("d")
    ignores_path = tmp_path / "ignores.txt"

    purchases = [_StubItem(item_id=1, band_name="X", item_title="Y", subdomain="x", slug="y")]
    stats = reconcile_with_bandcamp_purchases(
        tmp_path, cookies_path, ignores_path,
        bandcamp_factory=lambda _: _StubBandcamp(purchases),
        lookup_fn=lambda url: None,
    )
    assert stats.matched == 0
    assert stats.unmatched_purchases == 1


def test_phase_b_records_failed_lookups(tmp_path):
    from harmonist.mb_lookup import MBError

    cookies_path = tmp_path / "cookies.txt"
    cookies_path.write_text("d")
    ignores_path = tmp_path / "ignores.txt"

    def boom(_url):
        raise MBError("network down")

    purchases = [_StubItem(item_id=1, band_name="X", item_title="Y", subdomain="x", slug="y")]
    stats = reconcile_with_bandcamp_purchases(
        tmp_path, cookies_path, ignores_path,
        bandcamp_factory=lambda _: _StubBandcamp(purchases),
        lookup_fn=boom,
    )
    assert stats.failed_lookups == 1
    assert stats.matched == 0


def test_phase_b_idempotent_does_not_dupe_ignores(tmp_path):
    album_dir = _make_album(tmp_path, artist="A", album="B", mbid="rel-1")
    sc.write(album_dir, Sidecar(schema_version=1, source="manual", mb_release_id="rel-1"))

    cookies_path = tmp_path / "cookies.txt"
    cookies_path.write_text("d")
    ignores_path = tmp_path / "ignores.txt"

    purchases = [_StubItem(item_id=42, band_name="A", item_title="B",
                           subdomain="a", slug="b", item_type="album")]
    factory = lambda _: _StubBandcamp(purchases)
    lookup = lambda url: "rel-1"

    # First run
    s1 = reconcile_with_bandcamp_purchases(
        tmp_path, cookies_path, ignores_path,
        bandcamp_factory=factory, lookup_fn=lookup,
    )
    assert s1.matched == 1

    # Second run — already in ignores
    s2 = reconcile_with_bandcamp_purchases(
        tmp_path, cookies_path, ignores_path,
        bandcamp_factory=factory, lookup_fn=lookup,
    )
    assert s2.matched == 0
    assert s2.skipped_already_ignored == 1
    # And there's only one '42' line in the ignores file
    text = ignores_path.read_text()
    assert text.count("42  #") == 1


def test_phase_b_preserves_existing_ignores_content(tmp_path):
    album_dir = _make_album(tmp_path, artist="A", album="B", mbid="rel-1")
    sc.write(album_dir, Sidecar(schema_version=1, source="manual", mb_release_id="rel-1"))

    cookies_path = tmp_path / "cookies.txt"
    cookies_path.write_text("d")
    ignores_path = tmp_path / "ignores.txt"
    ignores_path.write_text(
        "# user-managed section\n"
        "999  # pre-existing ignore\n"
        "\n"
        "# IDs of items already downloaded will be automatically added below this line.\n"
        "# =========================================================\n"
        "888  # earlier auto-managed entry\n"
    )

    purchases = [_StubItem(item_id=42, band_name="A", item_title="B",
                           subdomain="a", slug="b")]
    reconcile_with_bandcamp_purchases(
        tmp_path, cookies_path, ignores_path,
        bandcamp_factory=lambda _: _StubBandcamp(purchases),
        lookup_fn=lambda url: "rel-1",
    )
    text = ignores_path.read_text()
    assert "999  # pre-existing ignore" in text
    assert "888  # earlier auto-managed entry" in text
    assert "42  # A / B" in text


# ============================================================================
# Orchestrator
# ============================================================================


def test_bootstrap_runs_phase_a_only_without_cookies(tmp_path):
    _make_album(tmp_path, artist="A", album="B", mbid="rel-1")
    result = bootstrap_fn(tmp_path)
    assert isinstance(result, BootstrapResult)
    assert result.phase_a.derived == 1
    assert result.phase_b is None
    assert result.errors == []


def test_bootstrap_runs_both_phases_when_cookies_present(tmp_path):
    album_dir = _make_album(tmp_path, artist="A", album="B", mbid="rel-1")
    cookies_path = tmp_path / "cookies.txt"
    cookies_path.write_text("d")
    ignores_path = tmp_path / "ignores.txt"

    purchases = [_StubItem(item_id=42, band_name="A", item_title="B",
                           subdomain="a", slug="b", item_type="album")]
    result = bootstrap_fn(
        tmp_path,
        cookies_path=cookies_path,
        ignores_path=ignores_path,
        bandcamp_factory=lambda _: _StubBandcamp(purchases),
        lookup_fn=lambda url: "rel-1",
    )
    assert result.phase_a.derived == 1
    assert result.phase_b is not None
    assert result.phase_b.matched == 1
    # Sidecar upgraded by Phase B after being created in Phase A
    loaded = sc.read(album_dir)
    assert loaded.source == "bandcamp"


def test_bootstrap_records_phase_b_errors(tmp_path):
    _make_album(tmp_path, artist="A", album="B", mbid="rel-1")
    cookies_path = tmp_path / "cookies.txt"
    cookies_path.write_text("d")
    ignores_path = tmp_path / "ignores.txt"

    def explode(_path):
        raise RuntimeError("bandcamp auth failed")

    result = bootstrap_fn(
        tmp_path,
        cookies_path=cookies_path,
        ignores_path=ignores_path,
        bandcamp_factory=explode,
    )
    assert result.phase_a.derived == 1  # Phase A still ran
    assert result.phase_b is None
    assert any("Phase B" in e for e in result.errors)
