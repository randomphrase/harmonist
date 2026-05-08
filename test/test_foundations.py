"""Smoke tests for chunk A foundations: config, models, sidecar."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from harmonist import config as config_mod
from harmonist import sidecar as sc
from harmonist.models import (
    Album,
    AlbumState,
    BandcampInfo,
    MatchCandidate,
    MBLookupAttempt,
    Sidecar,
    TrackComparison,
)


# ---------- config ----------

def test_config_loads_with_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("HARMONIST_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("HARMONIST_MUSIC_DIR", str(tmp_path / "music"))
    monkeypatch.setenv("HARMONIST_PORT", "9000")
    monkeypatch.setenv("HARMONIST_DOWNLOAD_FORMAT", "flac")
    cfg = config_mod.load()
    assert cfg.paths.config_dir == tmp_path / "cfg"
    assert cfg.paths.music_dir == tmp_path / "music"
    assert cfg.server.port == 9000
    assert cfg.bandcamp.download_format == "flac"


def test_config_defaults(monkeypatch, tmp_path):
    for var in ("HARMONIST_PORT", "HARMONIST_DOWNLOAD_FORMAT", "HARMONIST_TEST_MODE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HARMONIST_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("HARMONIST_MUSIC_DIR", str(tmp_path / "music"))
    cfg = config_mod.load()
    assert cfg.bandcamp.download_format == "alac"
    assert cfg.bandcamp.max_downloads_per_sync == 5
    assert cfg.musicbrainz.user_agent == "Harmonist/0.1 ( harmonist@girtby.net )"
    assert cfg.cover_art.size == "original"
    assert cfg.test.mode == "fixture"


def test_config_toml_overlay(monkeypatch, tmp_path):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "harmonist.toml").write_text(
        """
[bandcamp]
download_format = "flac"
max_downloads_per_sync = 2

[server]
port = 8765
"""
    )
    monkeypatch.setenv("HARMONIST_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("HARMONIST_MUSIC_DIR", str(tmp_path / "music"))
    for var in ("HARMONIST_PORT", "HARMONIST_DOWNLOAD_FORMAT", "HARMONIST_MAX_DOWNLOADS_PER_SYNC"):
        monkeypatch.delenv(var, raising=False)
    cfg = config_mod.load()
    assert cfg.bandcamp.download_format == "flac"
    assert cfg.bandcamp.max_downloads_per_sync == 2
    assert cfg.server.port == 8765


def test_config_env_beats_toml(monkeypatch, tmp_path):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "harmonist.toml").write_text(
        """
[server]
port = 8765
"""
    )
    monkeypatch.setenv("HARMONIST_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("HARMONIST_MUSIC_DIR", str(tmp_path / "music"))
    monkeypatch.setenv("HARMONIST_PORT", "9999")
    cfg = config_mod.load()
    assert cfg.server.port == 9999


def test_config_default_ignores_and_cookies(monkeypatch, tmp_path):
    monkeypatch.setenv("HARMONIST_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("HARMONIST_MUSIC_DIR", str(tmp_path / "music"))
    cfg = config_mod.load()
    assert cfg.ignores_file == tmp_path / "cfg" / "ignores.txt"
    assert cfg.cookies_file == tmp_path / "cfg" / "cookies.txt"


# ---------- models ----------

def test_album_id_is_md5_of_path():
    p = Path("/some/Artist/Album")
    assert Album.make_id(p) == "70aa67910d50fb00570339d2c9b42c7c"  # deterministic
    assert Album.make_id(p) == Album.make_id(p)  # stable across calls
    assert Album.make_id(p) != Album.make_id(Path("/other/Path"))


def test_album_state_values():
    assert AlbumState.ORPHAN.value == "orphan"
    assert AlbumState.HELD_BANDCAMP.value == "held_bandcamp"
    assert AlbumState.HELD_MANUAL.value == "held_manual"
    assert AlbumState.NEEDS_CONFIRMATION.value == "needs_confirmation"
    assert AlbumState.TAGGING.value == "tagging"
    assert AlbumState.DONE.value == "done"


# ---------- sidecar ----------

def test_sidecar_round_trip_bandcamp(tmp_path):
    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    s = Sidecar(
        schema_version=1,
        source="bandcamp",
        bandcamp=BandcampInfo(url="https://x.bandcamp.com/album/y", item_id=42, band_id=99),
        downloaded_at=datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc),
    )
    sc.write(album_dir, s)
    loaded = sc.read(album_dir)
    assert loaded is not None
    assert loaded.source == "bandcamp"
    assert loaded.bandcamp.url == s.bandcamp.url
    assert loaded.bandcamp.item_id == 42
    assert loaded.bandcamp.band_id == 99
    assert loaded.downloaded_at == s.downloaded_at


def test_sidecar_round_trip_manual_with_history(tmp_path):
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    s = Sidecar(
        schema_version=1,
        source="manual",
        added_at=datetime(2026, 5, 7, 13, 0, 0, tzinfo=timezone.utc),
        mb_release_id="abc-123",
        mb_last_checked_at=datetime(2026, 5, 7, 13, 0, 1, tzinfo=timezone.utc),
        mb_lookup_history=[
            MBLookupAttempt(
                at=datetime(2026, 5, 7, 13, 0, 1, tzinfo=timezone.utc),
                result="match",
                mbid="abc-123",
            )
        ],
        notes="seeded by hand",
    )
    sc.write(album_dir, s)
    loaded = sc.read(album_dir)
    assert loaded.mb_release_id == "abc-123"
    assert loaded.notes == "seeded by hand"
    assert len(loaded.mb_lookup_history) == 1
    assert loaded.mb_lookup_history[0].result == "match"


def test_sidecar_returns_none_when_absent(tmp_path):
    album_dir = tmp_path / "Empty"
    album_dir.mkdir()
    assert sc.read(album_dir) is None
    assert not sc.has_sidecar(album_dir)


def test_sidecar_rejects_unknown_schema(tmp_path):
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    sc.sidecar_path(album_dir).write_text(
        '{"schema_version": 99, "source": "bandcamp"}', encoding="utf-8"
    )
    with pytest.raises(sc.UnsupportedSchemaVersion):
        sc.read(album_dir)


def test_sidecar_rejects_invalid_json(tmp_path):
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    sc.sidecar_path(album_dir).write_text("not json {{{", encoding="utf-8")
    with pytest.raises(sc.InvalidSidecar):
        sc.read(album_dir)


def test_sidecar_rejects_invalid_source(tmp_path):
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    sc.sidecar_path(album_dir).write_text(
        '{"schema_version": 1, "source": "spotify"}', encoding="utf-8"
    )
    with pytest.raises(sc.InvalidSidecar):
        sc.read(album_dir)


def test_sidecar_atomic_write_no_tmp_leftover(tmp_path):
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    s = Sidecar(schema_version=1, source="manual", added_at=datetime.now(timezone.utc))
    sc.write(album_dir, s)
    assert not list(album_dir.glob("*.tmp"))
    assert sc.has_sidecar(album_dir)


def test_sidecar_round_trip_with_match_candidate(tmp_path):
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    candidate = MatchCandidate(
        mb_release_id="rel-zzz",
        confidence="approximate",
        file_count=2,
        track_count=2,
        track_comparisons=[
            TrackComparison(
                file_name="01.m4a",
                file_duration_ms=180000,
                file_title="Song A (file)",
                mb_track_title="Song A",
                mb_track_length_ms=185000,
                delta_ms=5000,
            ),
            TrackComparison(
                file_name="02.m4a",
                file_duration_ms=200000,
                file_title="Song B (file)",
                mb_track_title="Song B",
                mb_track_length_ms=None,
                delta_ms=None,
            ),
        ],
        proposed_at=datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc),
        notes=["some track lengths differ", "some MB tracks have no recorded length"],
    )
    s = Sidecar(
        schema_version=1,
        source="bandcamp",
        bandcamp=BandcampInfo(url="https://x.bandcamp.com/album/y", item_id=1),
        mb_match_candidate=candidate,
    )
    sc.write(album_dir, s)

    loaded = sc.read(album_dir)
    assert loaded.mb_release_id is None
    assert loaded.mb_match_candidate is not None
    c = loaded.mb_match_candidate
    assert c.mb_release_id == "rel-zzz"
    assert c.confidence == "approximate"
    assert c.file_count == 2
    assert c.track_count == 2
    assert len(c.track_comparisons) == 2
    assert c.track_comparisons[0].delta_ms == 5000
    assert c.track_comparisons[0].file_title == "Song A (file)"
    assert c.track_comparisons[1].mb_track_length_ms is None
    assert c.track_comparisons[1].delta_ms is None
    assert c.proposed_at == candidate.proposed_at
    assert c.notes == candidate.notes


def test_sidecar_history_bounded_to_last_10(tmp_path):
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    history = [
        MBLookupAttempt(
            at=datetime(2026, 5, i + 1, 0, 0, 0, tzinfo=timezone.utc),
            result="no_match",
        )
        for i in range(15)
    ]
    s = Sidecar(schema_version=1, source="bandcamp",
                bandcamp=BandcampInfo(url="https://x.bandcamp.com/album/y", item_id=1),
                mb_lookup_history=history)
    sc.write(album_dir, s)
    loaded = sc.read(album_dir)
    assert len(loaded.mb_lookup_history) == 10
    # Most recent 10 are kept (last 10 of 15)
    assert loaded.mb_lookup_history[0].at.day == 6
    assert loaded.mb_lookup_history[-1].at.day == 15
