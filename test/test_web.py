"""Smoke tests for the FastAPI layer.

Not exhaustive — task 13 owns the comprehensive integration test matrix.
These verify wiring: routes load, scanner integration works, state-dispatched
templates render without crashing for each AlbumState.
"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from mutagen.mp4 import MP4

from harmonist import sidecar as sc
from harmonist.config import Config, PathsConfig, BandcampConfig, ServerConfig, TestConfig
from harmonist.models import BandcampInfo, MatchCandidate, Sidecar, TrackComparison
from harmonist.web.main import create_app


FIXTURES_DIR = Path(__file__).parent / "fixtures"
SINE_M4A = FIXTURES_DIR / "sine.m4a"


@pytest.fixture
def cfg(tmp_path):
    return Config(
        paths=PathsConfig(
            config_dir=tmp_path / "config",
            music_dir=tmp_path / "music",
        ),
        bandcamp=BandcampConfig(),
        server=ServerConfig(),
        test=TestConfig(mode="fixture"),
    )


@pytest.fixture
def client(cfg):
    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.config_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(cfg)
    return TestClient(app)


def _make_album(cfg, name: str, *, mbid: str = None, comment: str = None) -> Path:
    d = cfg.paths.music_dir / "Artist" / name
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


# ---------- basic routes ----------

def test_index_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Inbox" in r.text


def test_tasks_empty_inbox(client):
    r = client.get("/tasks")
    assert r.status_code == 200
    assert "Inbox is empty" in r.text


def test_healthz(client, cfg):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["music_dir"] == str(cfg.paths.music_dir)
    assert body["sync_state"] == "idle"


def test_sync_status_idle(client):
    r = client.get("/sync/status")
    assert r.status_code == 200
    assert r.json()["state"] == "idle"


# ---------- state dispatch — each card type renders ----------

def test_orphan_card_rendered(client, cfg):
    _make_album(cfg, "Orphan Album")
    r = client.get("/tasks")
    assert "Orphan" in r.text
    assert "Reconcile" in r.text
    assert 'hx-post="/reconcile/' in r.text


def test_held_bandcamp_card_rendered(client, cfg):
    d = _make_album(cfg, "Held BC")
    sc.write(d, Sidecar(
        schema_version=1, source="bandcamp",
        bandcamp=BandcampInfo(url="https://x.bandcamp.com/album/y", item_id=1),
    ))
    r = client.get("/tasks")
    assert "Held (Bandcamp)" in r.text
    assert "Open in Harmony" in r.text
    assert "harmony.pulsewidth.org.uk" in r.text


def test_held_manual_card_rendered(client, cfg):
    d = _make_album(cfg, "Held Manual")
    sc.write(d, Sidecar(schema_version=1, source="manual"))
    r = client.get("/tasks")
    assert "Held (Manual)" in r.text


def test_needs_confirmation_card_renders_side_by_side(client, cfg):
    d = _make_album(cfg, "NC Album")
    sc.write(d, Sidecar(
        schema_version=1, source="bandcamp",
        bandcamp=BandcampInfo(url="https://x.bandcamp.com/album/y", item_id=1),
        mb_match_candidate=MatchCandidate(
            mb_release_id="rel-aaa",
            confidence="approximate",
            file_count=2, track_count=2,
            track_comparisons=[
                TrackComparison(file_name="01.m4a", file_duration_ms=180000,
                                file_title="Side A", mb_track_title="Side A",
                                mb_track_length_ms=185000, delta_ms=5000),
                TrackComparison(file_name="02.m4a", file_duration_ms=200000,
                                file_title="Side B", mb_track_title="Side B",
                                mb_track_length_ms=200500, delta_ms=500),
            ],
        ),
    ))
    r = client.get("/tasks")
    assert "Needs Confirmation" in r.text
    assert "approximate" in r.text
    assert "Side A" in r.text
    assert "Confirm" in r.text
    assert "Reject" in r.text


def test_unconfirmed_bandcamp_card_renders(client, cfg):
    d = _make_album(cfg, "UB Album")
    # Tag the file so scanner sees it as DONE-style (mb_release_id matches)
    audio = MP4(d / "01 Track.m4a")
    audio["----:com.apple.iTunes:MusicBrainz Album Id"] = [b"rel-aaa"]
    audio.save()
    sc.write(d, Sidecar(
        schema_version=1, source="bandcamp",
        bandcamp=BandcampInfo(url="https://x.bandcamp.com/album/y", item_id=None),
        mb_release_id="rel-aaa",
        tagged_at=datetime.now(timezone.utc),
    ))
    r = client.get("/tasks")
    assert "Unconfirmed Bandcamp" in r.text
    assert "Mark purchased elsewhere" in r.text
    assert 'hx-post="/unconfirmed/' in r.text


# ---------- action endpoints ----------

def test_post_sync_starts_runner(client):
    # Replace the runner_fn to a no-op so we don't try to hit Bandcamp
    runner = client.app.state.sync_runner
    runner._runner_fn = lambda: None
    r = client.post("/sync")
    assert r.status_code == 200
    assert "Sync started" in r.text


def test_post_sync_409_when_already_running(client):
    runner = client.app.state.sync_runner
    # Manually flag as running so the second POST hits the 409 branch
    runner._status.state = "running"
    r = client.post("/sync")
    assert r.status_code == 409
    assert "already running" in r.text


def test_reject_clears_candidate(client, cfg):
    d = _make_album(cfg, "RC")
    sc.write(d, Sidecar(
        schema_version=1, source="bandcamp",
        bandcamp=BandcampInfo(url="https://x.bandcamp.com/album/y", item_id=1),
        mb_match_candidate=MatchCandidate(
            mb_release_id="rel-zzz", confidence="approximate",
            file_count=1, track_count=1,
        ),
    ))
    from harmonist.models import Album
    aid = Album.make_id(d)
    r = client.post(f"/reject/{aid}")
    assert r.status_code == 200
    loaded = sc.read(d)
    assert loaded.mb_match_candidate is None


def test_unconfirmed_url_update(client, cfg):
    d = _make_album(cfg, "UB")
    audio = MP4(d / "01 Track.m4a")
    audio["----:com.apple.iTunes:MusicBrainz Album Id"] = [b"rel-aaa"]
    audio.save()
    sc.write(d, Sidecar(
        schema_version=1, source="bandcamp",
        bandcamp=BandcampInfo(url="https://x.bandcamp.com/album/old", item_id=None),
        mb_release_id="rel-aaa", tagged_at=datetime.now(timezone.utc),
    ))
    from harmonist.models import Album
    aid = Album.make_id(d)
    r = client.post(f"/unconfirmed/{aid}/url",
                    data={"url": "https://x.bandcamp.com/album/new"})
    assert r.status_code == 200
    loaded = sc.read(d)
    assert loaded.bandcamp.url == "https://x.bandcamp.com/album/new"
    assert loaded.bandcamp.item_id is None


def test_unconfirmed_mark_manual(client, cfg):
    d = _make_album(cfg, "UB2")
    audio = MP4(d / "01 Track.m4a")
    audio["----:com.apple.iTunes:MusicBrainz Album Id"] = [b"rel-aaa"]
    audio.save()
    sc.write(d, Sidecar(
        schema_version=1, source="bandcamp",
        bandcamp=BandcampInfo(url="https://x.bandcamp.com/album/y", item_id=None),
        mb_release_id="rel-aaa", tagged_at=datetime.now(timezone.utc),
    ))
    from harmonist.models import Album
    aid = Album.make_id(d)
    r = client.post(f"/unconfirmed/{aid}/manual")
    assert r.status_code == 200
    loaded = sc.read(d)
    assert loaded.source == "manual"
    assert loaded.bandcamp is None
    assert loaded.mb_release_id == "rel-aaa"  # preserved


def test_404_for_missing_album(client):
    r = client.post("/recheck/nonexistent")
    assert r.status_code == 404


# ---------- cover route ----------

def test_cover_returns_404_when_absent(client, cfg):
    d = _make_album(cfg, "NoCover")
    from harmonist.models import Album
    aid = Album.make_id(d)
    r = client.get(f"/cover/{aid}")
    assert r.status_code == 404


def test_cover_serves_when_present(client, cfg):
    d = _make_album(cfg, "WithCover")
    (d / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0FAKE")
    from harmonist.models import Album
    aid = Album.make_id(d)
    r = client.get(f"/cover/{aid}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content.startswith(b"\xff\xd8")
