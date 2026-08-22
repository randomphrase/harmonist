"""Smoke tests for chunk A foundations: config, models, sidecar."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from harmonist import config as config_mod
from harmonist import sidecar as sc
from harmonist.models import (
    AlbumState,
    BandcampInfo,
    MatchCandidate,
    Sidecar,
    TrackComparison,
    is_bandcamp_url,
    store_name,
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


def test_demo_mode_sandboxes_music_dir(monkeypatch, tmp_path):
    """In demo mode, load() ignores the configured music_dir and uses a temp
    sandbox so the real library is never touched."""
    monkeypatch.setenv("HARMONIST_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("HARMONIST_MUSIC_DIR", str(tmp_path / "real-music"))
    monkeypatch.setenv("HARMONIST_DEMO_MODE", "1")
    cfg = config_mod.load()
    assert cfg.demo_mode is True
    # NOT the configured music dir
    assert cfg.paths.music_dir != tmp_path / "real-music"
    assert cfg.paths.music_dir.name == "harmonist-demo"


def test_config_defaults(monkeypatch, tmp_path):
    for var in ("HARMONIST_PORT", "HARMONIST_DOWNLOAD_FORMAT", "HARMONIST_TEST_MODE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HARMONIST_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("HARMONIST_MUSIC_DIR", str(tmp_path / "music"))
    cfg = config_mod.load()
    assert cfg.bandcamp.download_format == "flac"
    assert cfg.bandcamp.max_downloads_per_sync == 5
    assert cfg.musicbrainz.user_agent == "Harmonist/1.0 ( harmonist@girtby.net )"
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


def test_the_state_values_are_the_status_json_wire_contract():
    """These strings are the wire spelling of a state, not just how the enum is
    written: `live_counts.to_status()` mirrors them as the keys of the /status
    JSON, which the poll in index.html reads (`data.counts.needs_sync`), and
    `AlbumState` is a StrEnum so anything that serialises one emits the value.

    Asserted as a whole set rather than name by name: the old form listed six of
    the seven, so INCONSISTENT could have been renamed without a word from it.

    `needs_sync` is the one to leave alone hardest. It DISPLAYS as "Needs Link"
    (models.py says so), which makes renaming the value to match look like
    tidying.
    """
    assert {s.value for s in AlbumState} == {
        "new",
        "needs_mbid",
        "tagging",
        "needs_sync",
        "complete",
        "incomplete",
        "inconsistent",
    }


# ---------- store URL host classification ----------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://bandcamp.com/album/y", "bandcamp"),
        ("https://x.bandcamp.com/album/y", "bandcamp"),
        ("https://www.beatport.com/release/x/1", "beatport"),
        ("https://beatport.com/release/x/1", "beatport"),
        ("https://www.discogs.com/release/1", "discogs"),
        # Lookalike domains must not be classified as the real store: a bare
        # `endswith("beatport.com")` accepts all three of these.
        ("https://notbandcamp.com/album/y", None),
        ("https://evilbeatport.com/release/x/1", None),
        ("https://notdiscogs.com/release/1", None),
        # Nor may the domain appear only in the path or query.
        ("https://evil.example/?ref=bandcamp.com", None),
        ("https://evil.example/discogs.com/release/1", None),
        ("https://example.com/x", None),
        ("not a url", None),
        ("", None),
        (None, None),
    ],
)
def test_store_name_requires_a_matching_host(url, expected):
    assert store_name(url) == expected


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://bandcamp.com", True),
        ("https://x.bandcamp.com/album/y", True),
        ("https://notbandcamp.com/album/y", False),
        ("https://evil.example/?ref=bandcamp.com", False),
        ("", False),
        (None, False),
    ],
)
def test_is_bandcamp_url(url, expected):
    assert is_bandcamp_url(url) is expected


# ---------- temp_uid lifecycle ----------


def test_sidecar_write_mints_temp_uid_when_no_mbid(tmp_path):
    """Writing a sidecar with no mb_release_id auto-mints a temp_uid."""
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    s = Sidecar(store_url="https://x.bandcamp.com/album/y")
    sc.write(album_dir, s)
    loaded = sc.read(album_dir)
    assert loaded.temp_uid is not None
    assert len(loaded.temp_uid) == 32  # uuid4().hex
    assert loaded.mb_release_id is None


def test_sidecar_write_drops_stale_temp_uid_when_mbid_set(tmp_path):
    """Writing with mb_release_id set clears any stale temp_uid."""
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    s = Sidecar(
        mb_release_id="rel-aaa",
        temp_uid="stale-uid-from-earlier",
        tagged_at=datetime.now(UTC),
    )
    sc.write(album_dir, s)
    loaded = sc.read(album_dir)
    assert loaded.mb_release_id == "rel-aaa"
    assert loaded.temp_uid is None
    # JSON should not contain temp_uid at all when mbid is set
    raw = sc.sidecar_path(album_dir).read_text()
    assert "temp_uid" not in raw


def test_sidecar_write_preserves_existing_temp_uid(tmp_path):
    """A sidecar with a temp_uid and no mbid keeps the same uid on rewrite."""
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    sc.write(
        album_dir,
        Sidecar(store_url="https://x.bandcamp.com/album/y"),
    )
    first = sc.read(album_dir)
    # Rewrite (e.g. user updates store_url) — temp_uid should not regenerate
    sc.write(
        album_dir,
        Sidecar(
            store_url="https://x.bandcamp.com/album/different",
            temp_uid=first.temp_uid,
        ),
    )
    second = sc.read(album_dir)
    assert second.temp_uid == first.temp_uid


def test_a_retired_field_in_an_existing_sidecar_is_ignored(tmp_path):
    """The deprecation contract for #195, and for every field retired after it.

    `track_count_expected` shipped from v1.0.0 to v1.9.0, so it is in sidecars
    on real users' disks. Retiring it must not make those unreadable — which is
    what a `schema_version` bump would have done, since `_from_dict` tests exact
    equality and the scanner drops any album it cannot parse. Unknown keys are
    ignored instead, so the stale value simply loads as absent and disappears on
    the next write.
    """
    import json

    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    sc.write(
        album_dir, Sidecar(mb_release_id="rel-aaa", tagged_at=datetime(2026, 5, 7, tzinfo=UTC))
    )

    # A sidecar as an older Harmonist left it.
    path = sc.sidecar_path(album_dir)
    payload = json.loads(path.read_text())
    payload["track_count_expected"] = 12
    path.write_text(json.dumps(payload))

    loaded = sc.read(album_dir)
    assert loaded is not None, "a stale key must not make the album unreadable"
    assert loaded.mb_release_id == "rel-aaa"
    assert not hasattr(loaded, "track_count_expected")

    # And it is gone once the sidecar is rewritten for any other reason.
    sc.write(album_dir, loaded)
    assert "track_count_expected" not in json.loads(path.read_text())


def test_sidecar_round_trip_purchase_unavailable(tmp_path):
    """purchase_unavailable persists (and defaults False when absent)."""
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    sc.write(
        album_dir,
        Sidecar(
            mb_release_id="rel-aaa",
            tagged_at=datetime(2026, 5, 7, tzinfo=UTC),
            purchase_unavailable=True,
        ),
    )
    assert sc.read(album_dir).purchase_unavailable is True
    # Absent in an older sidecar → False.
    sc.write(album_dir, Sidecar(mb_release_id="rel-bbb"))
    assert sc.read(album_dir).purchase_unavailable is False


def test_sidecar_read_rejects_both_mbid_and_temp_uid(tmp_path):
    """Hand-crafted sidecar with both identity fields set is invalid."""
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    sc.sidecar_path(album_dir).write_text(
        '{"schema_version": 1, "mb_release_id": "rel-x", "temp_uid": "uid-y"}',
        encoding="utf-8",
    )
    with pytest.raises(sc.InvalidSidecarError, match="mutually exclusive"):
        sc.read(album_dir)


def test_sidecar_round_trip_with_optional_item_id(tmp_path):
    """item_id is None when we know the URL but haven't reconciled with purchases."""
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    s = Sidecar(
        store_url="https://x.bandcamp.com/album/y",
        bandcamp=BandcampInfo(item_id=None),
        mb_release_id="rel-aaa",
    )
    sc.write(album_dir, s)
    loaded = sc.read(album_dir)
    assert loaded.store_url == "https://x.bandcamp.com/album/y"
    assert loaded.bandcamp is None or loaded.bandcamp.item_id is None
    # JSON should not include item_id when None
    raw = sc.sidecar_path(album_dir).read_text()
    assert "item_id" not in raw


# ---------- sidecar ----------


def test_sidecar_round_trip_bandcamp(tmp_path):
    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    s = Sidecar(
        store_url="https://x.bandcamp.com/album/y",
        bandcamp=BandcampInfo(item_id=42, band_id=99),
        downloaded_at=datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC),
    )
    sc.write(album_dir, s)
    loaded = sc.read(album_dir)
    assert loaded is not None
    assert loaded.store_url == s.store_url
    assert loaded.bandcamp.item_id == 42
    assert loaded.bandcamp.band_id == 99
    assert loaded.downloaded_at == s.downloaded_at


def test_sidecar_round_trip_ambiguous_candidate_ids(tmp_path):
    """An ambiguous Bandcamp link (no item_id, a set of candidate ids) survives
    a write/read round-trip."""
    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    s = Sidecar(
        store_url="https://x.bandcamp.com/album/y",
        bandcamp=BandcampInfo(item_id=None, candidate_item_ids=[222, 111]),
        mb_release_id="rel-x",
    )
    sc.write(album_dir, s)
    loaded = sc.read(album_dir)
    assert loaded.bandcamp.item_id is None
    assert loaded.bandcamp.candidate_item_ids == [222, 111]


def test_sidecar_round_trip_manual(tmp_path):
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    s = Sidecar(
        added_at=datetime(2026, 5, 7, 13, 0, 0, tzinfo=UTC),
        mb_release_id="abc-123",
        notes="seeded by hand",
    )
    sc.write(album_dir, s)
    loaded = sc.read(album_dir)
    assert loaded.mb_release_id == "abc-123"
    assert loaded.notes == "seeded by hand"


def test_sidecar_returns_none_when_absent(tmp_path):
    album_dir = tmp_path / "Empty"
    album_dir.mkdir()
    assert sc.read(album_dir) is None
    assert not sc.has_sidecar(album_dir)


def test_sidecar_rejects_unknown_schema(tmp_path):
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    sc.sidecar_path(album_dir).write_text('{"schema_version": 99}', encoding="utf-8")
    with pytest.raises(sc.UnsupportedSchemaVersionError):
        sc.read(album_dir)


def test_sidecar_rejects_invalid_json(tmp_path):
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    sc.sidecar_path(album_dir).write_text("not json {{{", encoding="utf-8")
    with pytest.raises(sc.InvalidSidecarError):
        sc.read(album_dir)


def test_sidecar_atomic_write_no_tmp_leftover(tmp_path):
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    s = Sidecar(added_at=datetime.now(UTC))
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
        proposed_at=datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC),
        notes=["some track lengths differ", "some MB tracks have no recorded length"],
    )
    s = Sidecar(
        store_url="https://x.bandcamp.com/album/y",
        bandcamp=BandcampInfo(item_id=1),
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


def test_sidecar_round_trip_tracks_unavailable(tmp_path):
    """Accepting an incomplete album as finished persists, and defaults False
    when the key is absent — an older sidecar has never heard of it (#196)."""
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    sc.write(album_dir, Sidecar(mb_release_id="rel-aaa", tracks_unavailable=True))
    assert sc.read(album_dir).tracks_unavailable is True

    plain = tmp_path / "Plain"
    plain.mkdir()
    sc.write(plain, Sidecar(mb_release_id="rel-bbb"))
    assert sc.read(plain).tracks_unavailable is False


def test_the_default_is_omitted_from_the_sidecar_file(tmp_path):
    """Sidecars are read by people debugging their own libraries; a file listing
    every field at its default hides the ones that matter."""
    import json

    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    sc.write(album_dir, Sidecar(mb_release_id="rel-aaa"))

    assert "tracks_unavailable" not in json.loads(sc.sidecar_path(album_dir).read_text())
