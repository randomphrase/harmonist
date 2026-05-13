"""Tests for the background reconciliation runner + auto-trigger on /tasks."""
from __future__ import annotations

import shutil
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mutagen.mp4 import MP4

from harmonist import sidecar as sc
from harmonist.config import (
    BandcampConfig,
    Config,
    MusicBrainzConfig,
    PathsConfig,
    ServerConfig,
    TestConfig,
)
from harmonist.models import AlbumState
from harmonist.web.main import create_app
from harmonist.web.reconcile_runner import (
    ReconcileRunner,
    ReconcileStatus,
    reconcile_pending_orphans,
)


FIXTURES_DIR = Path(__file__).parent / "fixtures"
SINE_M4A = FIXTURES_DIR / "sine.m4a"


def _make_album(root: Path, name: str, *, mbid: str | None = None, comment: str | None = None) -> Path:
    d = root / "Artist" / name
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


# ---------- ReconcileRunner unit ----------

def test_runner_starts_idle():
    runner = ReconcileRunner(runner_fn=lambda updater: None)
    assert runner.is_running is False
    assert runner.status()["state"] == "idle"


def test_runner_runs_runner_fn():
    called = []
    def fn(updater):
        called.append(True)
    runner = ReconcileRunner(runner_fn=fn)
    assert runner.start() is True
    # Wait for the thread
    for _ in range(50):
        if not runner.is_running:
            break
        time.sleep(0.02)
    assert called == [True]
    assert runner.status()["state"] == "idle"


def test_runner_refuses_concurrent_start():
    def slow_fn(updater):
        time.sleep(0.1)
    runner = ReconcileRunner(runner_fn=slow_fn)
    assert runner.start() is True
    # Second start should refuse while first is running
    assert runner.start() is False
    # Wait for completion
    for _ in range(50):
        if not runner.is_running:
            break
        time.sleep(0.02)


def test_runner_debounces_back_to_back_starts():
    runner = ReconcileRunner(runner_fn=lambda updater: None)
    assert runner.start() is True
    # Wait for finish
    for _ in range(50):
        if not runner.is_running:
            break
        time.sleep(0.02)
    # Within the debounce window, can't restart
    assert runner.start() is False
    # Force the finished_at back so debounce window passes
    runner._status.finished_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    assert runner.start() is True


def test_runner_captures_exception_in_status():
    def boom(updater):
        raise RuntimeError("kaboom")
    runner = ReconcileRunner(runner_fn=boom)
    runner.start()
    for _ in range(50):
        if not runner.is_running:
            break
        time.sleep(0.02)
    assert "kaboom" in runner.status()["last_error"]


def test_runner_status_updater_propagates_to_status():
    def fn(updater):
        updater(total=3)
        updater(current_item="Album A", completed=1)
        updater(current_item="Album B", completed=2)
    runner = ReconcileRunner(runner_fn=fn)
    runner.start()
    for _ in range(50):
        if not runner.is_running:
            break
        time.sleep(0.02)
    s = runner.status()
    # current_item gets cleared back to "" on completion
    assert s["total"] == 3
    assert s["completed"] == 2


# ---------- reconcile_pending_orphans ----------

def test_reconcile_pending_walks_only_orphans(tmp_path):
    music = tmp_path / "music"
    orphan_with_mbid = _make_album(
        music, "OrphanWithMBID", mbid="rel-1",
        comment="Visit https://x.bandcamp.com",
    )
    orphan_without_mbid = _make_album(music, "OrphanNoMBID")
    held = _make_album(music, "Held", mbid="rel-h")
    sc.write(held, sc_for_held())

    seen = []
    stats = reconcile_pending_orphans(
        music,
        fetch_urls=lambda mbid: (
            ["https://x.bandcamp.com/album/y"] if mbid == "rel-1" else []
        ),
        rate_limit_seconds=0,  # fast tests
        status_updater=lambda **kw: seen.append(kw),
    )
    # The orphan-with-MBID got a bandcamp sidecar
    loaded = sc.read(orphan_with_mbid)
    assert loaded is not None
    assert loaded.source == "bandcamp"
    # The orphan without MBID had no sidecar written (reconcile_album returns None)
    assert not sc.has_sidecar(orphan_without_mbid)
    # Held wasn't touched
    assert sc.read(held).source == "manual"

    assert stats["total"] == 2  # two orphans
    assert stats["reconciled_bandcamp"] == 1
    assert stats["skipped"] == 1


def sc_for_held():
    from harmonist.models import Sidecar
    return Sidecar(schema_version=1, source="manual", mb_release_id="rel-h")


# ---------- web integration ----------

@pytest.fixture
def client(tmp_path):
    cfg = Config(
        paths=PathsConfig(config_dir=tmp_path / "cfg", music_dir=tmp_path / "music"),
        bandcamp=BandcampConfig(),
        musicbrainz=MusicBrainzConfig(),
        server=ServerConfig(),
        test=TestConfig(mode="fixture"),
    )
    cfg.paths.config_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    return TestClient(create_app(cfg))


def test_reconcile_status_endpoint_returns_json(client):
    r = client.get("/reconcile/status")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "idle"
    assert "current_item" in body
    assert "completed" in body
    assert "total" in body


def test_tasks_kicks_reconcile_runner_when_orphans_present(client, tmp_path, monkeypatch):
    music_dir = tmp_path / "music"
    _make_album(music_dir, "Orphan", mbid="rel-1", comment="https://x.bandcamp.com")

    # Spy on .start() to verify it gets called
    started = []
    orig_start = client.app.state.reconcile_runner.start

    def spy():
        started.append(True)
        return orig_start()

    monkeypatch.setattr(client.app.state.reconcile_runner, "start", spy)
    client.get("/tasks")
    assert started == [True]


def test_tasks_does_not_kick_when_no_orphans(client, tmp_path, monkeypatch):
    """No orphans = no need to reconcile."""
    started = []
    monkeypatch.setattr(
        client.app.state.reconcile_runner,
        "start",
        lambda: started.append(True) or False,
    )
    client.get("/tasks")
    assert started == []


def test_manual_post_reconcile_starts_runner(client, tmp_path):
    _make_album(tmp_path / "music", "Orphan", mbid="rel-1")
    r = client.post("/reconcile")
    assert r.status_code == 200
    assert "Reconcile started" in r.text or "already running" in r.text


def test_post_reconcile_returns_warning_when_in_debounce(client, monkeypatch):
    # Force the runner to refuse start
    monkeypatch.setattr(
        client.app.state.reconcile_runner, "start", lambda: False
    )
    r = client.post("/reconcile")
    assert r.status_code == 200
    assert "already running" in r.text.lower() or "just finished" in r.text.lower()
