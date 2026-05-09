"""Basic tests for demo mode."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harmonist import demo, scanner
from harmonist import sidecar as sc
from harmonist.config import Config, PathsConfig, BandcampConfig, ServerConfig, TestConfig
from harmonist.models import AlbumState
from harmonist.web.main import create_app


@pytest.fixture
def music_dir(tmp_path):
    return tmp_path / "music"


@pytest.fixture(autouse=True)
def reset_pending_queue():
    """Each test starts with a fresh pending queue."""
    demo._pending_queue = list(demo.PENDING_PURCHASES)
    yield


@pytest.fixture(autouse=True)
def restore_module_globals():
    """`demo.install()` monkey-patches mb_lookup / mb_search / cover_art at
    module level. Capture originals before each test and restore after, so
    demo tests can't leak patches into the rest of the suite.
    """
    from harmonist import cover_art, mb_lookup, mb_search
    saved = {
        "mb_lookup.fetch_release": mb_lookup.fetch_release,
        "mb_lookup.fetch_release_urls": mb_lookup.fetch_release_urls,
        "mb_lookup.lookup_by_bandcamp_url": mb_lookup.lookup_by_bandcamp_url,
        "mb_search.search_releases": mb_search.search_releases,
        "cover_art.ensure_cover": cover_art.ensure_cover,
    }
    yield
    mb_lookup.fetch_release = saved["mb_lookup.fetch_release"]
    mb_lookup.fetch_release_urls = saved["mb_lookup.fetch_release_urls"]
    mb_lookup.lookup_by_bandcamp_url = saved["mb_lookup.lookup_by_bandcamp_url"]
    mb_search.search_releases = saved["mb_search.search_releases"]
    cover_art.ensure_cover = saved["cover_art.ensure_cover"]


def test_seed_writes_marker_and_albums(music_dir):
    demo.seed(music_dir)
    assert demo.is_demo_dir(music_dir)
    # All 5 LIBRARY entries materialised
    artists = sorted(p.name for p in music_dir.iterdir() if p.is_dir())
    assert "Wyld Stallyns" in artists
    assert "Sex Bob-omb" in artists
    assert "Spinal Tap" in artists
    assert "The Wonders" in artists
    assert "Stillwater" in artists


def test_seed_produces_each_state(music_dir):
    demo.seed(music_dir)
    albums = scanner.scan(music_dir)
    states = {a.title: a.state for a in albums}
    assert states["Be Excellent"] == AlbumState.ORPHAN
    assert states["Threshold"] == AlbumState.HELD_BANDCAMP
    assert states["Smell the Glove"] == AlbumState.NEEDS_CONFIRMATION
    assert states["One Hit Wonderland"] == AlbumState.UNCONFIRMED_BANDCAMP
    assert states["Highway Hymns"] == AlbumState.DONE


def test_seed_orphan_has_mbid_and_comment_for_reconcile(music_dir):
    """The Orphan should be reconcile-able (MBID atom + Bandcamp ©cmt)."""
    demo.seed(music_dir)
    from mutagen.mp4 import MP4
    track = next((music_dir / "Wyld Stallyns" / "Be Excellent").glob("*.m4a"))
    audio = MP4(track)
    assert "----:com.apple.iTunes:MusicBrainz Album Id" in audio
    assert "bandcamp.com" in audio["\xa9cmt"][0]


def test_seed_writes_cover_jpgs(music_dir):
    demo.seed(music_dir)
    for artist_dir in music_dir.iterdir():
        if artist_dir.is_dir():
            for album_dir in artist_dir.iterdir():
                if album_dir.is_dir():
                    assert (album_dir / "cover.jpg").exists()


def test_reset_refuses_when_marker_missing(music_dir):
    music_dir.mkdir()
    (music_dir / "MyRealMusic").mkdir()
    (music_dir / "MyRealMusic" / "track.m4a").write_bytes(b"important data")
    with pytest.raises(RuntimeError, match="not a demo dir"):
        demo.reset(music_dir)
    # Untouched
    assert (music_dir / "MyRealMusic" / "track.m4a").exists()


def test_reset_wipes_and_reseeds(music_dir):
    demo.seed(music_dir)
    # Drop a stray file the user might have created during play
    (music_dir / "Wyld Stallyns" / "stray.txt").write_text("user added this")
    demo.reset(music_dir)
    assert not (music_dir / "Wyld Stallyns" / "stray.txt").exists()
    # All seeded albums back
    assert (music_dir / "Wyld Stallyns" / "Be Excellent").exists()


def test_reset_resets_pending_queue(music_dir):
    demo.seed(music_dir)
    demo.run_demo_sync(music_dir)  # pop one
    assert len(demo._pending_queue) == len(demo.PENDING_PURCHASES) - 1
    demo.reset(music_dir)
    assert len(demo._pending_queue) == len(demo.PENDING_PURCHASES)


def test_run_demo_sync_pops_one_pending(music_dir):
    demo.seed(music_dir)
    initial_albums = {a.path.name for a in scanner.scan(music_dir)}
    result = demo.run_demo_sync(music_dir)
    assert result.new_items_downloaded is True
    after = {a.path.name for a in scanner.scan(music_dir)}
    assert len(after) == len(initial_albums) + 1


def test_run_demo_sync_no_op_when_queue_empty(music_dir):
    demo.seed(music_dir)
    # Drain the queue
    while demo._pending_queue:
        demo.run_demo_sync(music_dir)
    result = demo.run_demo_sync(music_dir)
    assert result.new_items_downloaded is False


# ---------- mock service implementations ----------


def test_fetch_release_returns_demo_data():
    rel = demo.fetch_release("demo-rel-spinal-tap")
    assert rel["title"] == "Smell the Glove"


def test_fetch_release_unknown_mbid_raises():
    from harmonist.mb_lookup import MBError
    with pytest.raises(MBError):
        demo.fetch_release("not-a-real-demo-mbid")


def test_lookup_by_bandcamp_url():
    assert demo.lookup_by_bandcamp_url("https://spinaltap.bandcamp.com/album/smell-the-glove") == "demo-rel-spinal-tap"
    assert demo.lookup_by_bandcamp_url("https://example.com/whatever") is None


def test_fetch_release_urls_returns_bandcamp_url():
    urls = demo.fetch_release_urls("demo-rel-wyld-stallyns")
    assert urls == ["https://wyldstallyns.bandcamp.com/album/be-excellent"]


def test_search_releases_substring_match():
    results = demo.search_releases("Spinal", "")
    assert any(r["artist"] == "Spinal Tap" for r in results)


def test_search_releases_empty_inputs_returns_all():
    results = demo.search_releases("", "")
    # Empty inputs match everything (a-match and t-match both true)
    assert len(results) == len(demo.MB_RELEASES)


def test_ensure_cover_returns_existing(music_dir):
    album_dir = music_dir / "alb"
    album_dir.mkdir(parents=True)
    (album_dir / "cover.jpg").write_bytes(b"existing")
    assert demo.ensure_cover(album_dir, release_mbid="demo-rel-x") == album_dir / "cover.jpg"


def test_ensure_cover_copies_placeholder(music_dir):
    album_dir = music_dir / "alb"
    album_dir.mkdir(parents=True)
    result = demo.ensure_cover(album_dir, release_mbid="demo-rel-x")
    assert result == album_dir / "cover.jpg"
    assert result.exists()


# ---------- web integration ----------


@pytest.fixture
def demo_client(tmp_path):
    cfg = Config(
        paths=PathsConfig(config_dir=tmp_path / "cfg", music_dir=tmp_path / "music"),
        bandcamp=BandcampConfig(),
        server=ServerConfig(),
        test=TestConfig(mode="fixture"),
        demo_mode=True,
    )
    cfg.paths.config_dir.mkdir(parents=True, exist_ok=True)
    return TestClient(create_app(cfg))


def test_demo_mode_seeds_on_startup(demo_client):
    r = demo_client.get("/")
    assert r.status_code == 200
    assert "Demo Mode" in r.text
    # Inbox has the seeded albums
    tasks = demo_client.get("/tasks")
    assert "Wyld Stallyns" in tasks.text
    assert "Sex Bob-omb" in tasks.text


def test_demo_reset_endpoint(demo_client):
    r = demo_client.post("/demo/reset")
    assert r.status_code == 200
    assert "reset" in r.text.lower()


def test_demo_mode_off_no_demo_routes(tmp_path):
    cfg = Config(
        paths=PathsConfig(config_dir=tmp_path / "cfg", music_dir=tmp_path / "music"),
        bandcamp=BandcampConfig(),
        server=ServerConfig(),
        test=TestConfig(mode="fixture"),
        demo_mode=False,
    )
    cfg.paths.config_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    client = TestClient(create_app(cfg))
    r = client.post("/demo/reset")
    assert r.status_code == 404
    # No banner
    home = client.get("/")
    assert "Demo Mode" not in home.text
