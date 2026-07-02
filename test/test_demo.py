"""Basic tests for demo mode."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from harmonist import demo, scanner
from harmonist import sidecar as sc
from harmonist.config import BandcampConfig, Config, PathsConfig, ServerConfig, TestConfig
from harmonist.models import AlbumState
from harmonist.tagger import ATOM_COMMENT, ATOM_MB_ALBUM_ID
from harmonist.web.main import create_app


@pytest.fixture
def music_dir(tmp_path):
    return tmp_path / "music"


@pytest.fixture(autouse=True)
def reset_pending_downloads():
    """Each test starts (and ends) with clean potential-download state."""
    demo.pending_downloads.reset()
    yield
    demo.pending_downloads.reset()


@pytest.fixture(autouse=True)
def disable_sync_delays(monkeypatch):
    """Don't wait between demo sync steps in tests."""
    monkeypatch.setattr(demo, "STEP_DELAY_SECONDS", 0)


def test_seed_writes_marker_and_albums(music_dir):
    demo.seed(music_dir)
    assert demo.is_demo_dir(music_dir)
    # All 6 LIBRARY entries materialised
    artists = sorted(p.name for p in music_dir.iterdir() if p.is_dir())
    assert "Wyld Stallion" in artists
    assert "Sex Bob-omb" in artists
    assert "Sonic Death Monkey" in artists
    assert "The Thamesmen" in artists
    assert "Dingoes Ate My Baby" in artists
    assert "Various Artists" in artists


def test_seed_produces_each_state(music_dir):
    demo.seed(music_dir)
    albums = scanner.scan(music_dir)
    states = {a.title: a.state for a in albums}
    assert states["A Most Excellent Journey"] == AlbumState.NEW
    assert states["We Are Here To Make You Sad"] == AlbumState.NEEDS_MBID
    assert states["Top 5 Records For A Wednesday"] == AlbumState.NEEDS_MBID
    assert states["Gimme Some Money"] == AlbumState.NEEDS_MBID
    assert states["Little Bit o' Hoot, Whole Lotta Nanny"] == AlbumState.NEEDS_SYNC
    assert states["The Rural Juror (OST)"] == AlbumState.COMPLETE
    assert states["Can You Picture That?"] == AlbumState.INCOMPLETE
    # Fever Dog: seeded as a mis-tag (NEEDS_MBID with a mistag candidate).
    assert states["Fever Dog"] == AlbumState.NEEDS_MBID
    # The Awesome Album: non-Bandcamp comment → Library, unlinked (COMPLETE).
    assert states["The Awesome Album"] == AlbumState.COMPLETE


def test_seed_new_has_mbid_and_comment_for_reconcile(music_dir):
    """The new album should be reconcile-able (MBID atom + Bandcamp ©cmt)."""
    demo.seed(music_dir)
    from mutagen.mp4 import MP4

    track = next((music_dir / "Wyld Stallion" / "A Most Excellent Journey").glob("*.m4a"))
    audio = MP4(track)
    assert ATOM_MB_ALBUM_ID in audio
    assert "bandcamp.com" in audio[ATOM_COMMENT][0]


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
    (music_dir / "Wyld Stallion" / "stray.txt").write_text("user added this")
    demo.reset(music_dir)
    assert not (music_dir / "Wyld Stallion" / "stray.txt").exists()
    # All seeded albums back
    assert (music_dir / "Wyld Stallion" / "A Most Excellent Journey").exists()


def test_reset_clears_pending_downloads(music_dir):
    demo.seed(music_dir)
    demo.pending_downloads.replace_all(
        [demo.PendingPurchase(item_id=1, band="a", title="b", url="u", fmt="alac")]
    )
    demo.reset(music_dir)
    assert demo.pending_downloads.count() == 0


def test_link_only_sync_surfaces_potential_downloads(music_dir):
    """A link-only sync surfaces the owned-but-not-on-disk purchases as potential
    downloads (Mouse Rat matches a Library album; CB4 + Autobahn are new) and
    downloads nothing."""
    demo.seed(music_dir)
    before = {a.path.name for a in scanner.scan(music_dir)}
    result = demo.run_demo_sync(music_dir, link_only=True)
    assert result.new_items_downloaded is False
    pending = {p.title for p in demo.pending_downloads.all_pending()}
    assert "The Awesome Album" in pending  # Mouse Rat — recovered via fuzzy match
    assert "Straight Outta Lowcash" in pending  # CB4 — genuinely new
    assert "Nagelbett" in pending  # Autobahn
    # Nothing downloaded — no new album directories.
    assert {a.path.name for a in scanner.scan(music_dir)} == before


def test_full_sync_downloads_unmatched_purchases(music_dir):
    demo.seed(music_dir)
    result = demo.run_demo_sync(music_dir, link_only=False)
    assert result.new_items_downloaded is True
    after = {a.path.name for a in scanner.scan(music_dir)}
    assert "Straight Outta Lowcash" in after  # CB4 downloaded
    assert "Nagelbett" in after  # Autobahn downloaded
    assert demo.pending_downloads.count() == 0  # nothing left pending


def test_approved_potential_download_fetches_on_next_link_only_sync(music_dir):
    """Clicking Download on a potential-download approves it; the next link-only
    sync fetches just that one while the rest stay pending."""
    demo.seed(music_dir)
    demo.run_demo_sync(music_dir, link_only=True)  # surface them
    demo.pending_downloads.approve(2001)  # user chose Download on CB4
    demo.run_demo_sync(music_dir, link_only=True)
    after = {a.path.name for a in scanner.scan(music_dir)}
    assert "Straight Outta Lowcash" in after  # CB4 fetched
    assert "Nagelbett" not in after  # Autobahn still pending
    assert 2001 not in {p.item_id for p in demo.pending_downloads.all_pending()}


def test_run_demo_sync_reports_progress(music_dir):
    """Demo sync invokes the progress callback: item_id link-ins for existing
    albums, then downloads in a full sync."""
    demo.seed(music_dir)
    seen = []
    demo.run_demo_sync(
        music_dir, link_only=False, progress_callback=lambda label: seen.append(label)
    )
    assert any("CB4" in lbl for lbl in seen)  # a download step
    assert any("Dingoes" in lbl for lbl in seen)  # a Needs-Link → linked step


def test_run_demo_sync_links_existing_needs_sync_album(music_dir):
    """An album seeded as NEEDS_SYNC (matching Bandcamp URL, no item_id) gets its
    item_id filled in by sync — even a link-only run — without downloading."""
    demo.seed(music_dir)
    dingoes_dir = next(d for d in (music_dir / "Dingoes Ate My Baby").iterdir() if d.is_dir())
    before = sc.read(dingoes_dir)
    assert before.bandcamp is None or before.bandcamp.item_id is None

    demo.run_demo_sync(music_dir, link_only=True)

    after = sc.read(dingoes_dir)
    assert after.bandcamp is not None
    assert after.bandcamp.item_id == 1004


# ---------- mock service implementations ----------


def test_fetch_release_returns_demo_data():
    rel = demo.fetch_release("demo-rel-thamesmen")
    assert rel["title"] == "Gimme Some Money"


def test_fetch_release_unknown_mbid_raises():
    from harmonist.mb_lookup import MBError

    with pytest.raises(MBError):
        demo.fetch_release("not-a-real-demo-mbid")


def test_lookup_by_bandcamp_url():
    assert demo.lookup_by_bandcamp_url("https://thamesmen.bandcamp.com/album/gimme-some-money") == [
        "demo-rel-thamesmen"
    ]
    assert demo.lookup_by_bandcamp_url("https://example.com/whatever") == []


def test_fetch_release_urls_returns_bandcamp_url():
    urls = demo.fetch_release_urls("demo-rel-wyld")
    assert urls == ["https://wyldstallion.bandcamp.com/album/a-most-excellent-journey"]


def test_search_releases_substring_match():
    results = demo.search_releases("Thamesmen", "")
    assert any(r["artist"] == "The Thamesmen" for r in results)


def test_search_releases_empty_inputs_returns_all():
    # Empty inputs match everything (a-match and t-match both true); raise the
    # limit above the dataset size so nothing is capped.
    results = demo.search_releases("", "", limit=100)
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
    return TestClient(create_app(cfg), headers={"HX-Request": "true"})


def test_demo_mode_seeds_on_startup(demo_client):
    r = demo_client.get("/")
    assert r.status_code == 200
    assert "Demo Mode" in r.text
    # Inbox has the seeded albums
    tasks = demo_client.get("/tasks")
    assert "Wyld Stallion" in tasks.text
    assert "Sex Bob-omb" in tasks.text
    assert "Sonic Death Monkey" in tasks.text
    assert "The Thamesmen" in tasks.text
    assert "Dingoes Ate My Baby" in tasks.text
    # Done album hidden
    assert "Various Artists" not in tasks.text


def test_demo_reset_endpoint(demo_client):
    r = demo_client.post("/demo/reset")
    assert r.status_code == 200
    assert "reset" in r.text.lower()


def test_demo_confirm_tags_album_end_to_end(demo_client):
    """Click Confirm on Thamesmen — exercises fetch_release + tagger + cover."""
    tasks = demo_client.get("/tasks").text
    # Find the Thamesmen card; pull its album id out of the data-attributes
    import re

    m = re.search(r'task-([0-9a-f]{32})"[^"]*"[^>]*>[^<]*<[^>]*Gimme Some Money', tasks)
    if not m:
        # fallback: pick the album id by scanning
        from harmonist.models import AlbumState
        from harmonist.scanner import scan

        albums = scan(demo_client.app.state.cfg.paths.music_dir)
        nc = next(
            a
            for a in albums
            if a.state == AlbumState.NEEDS_MBID and a.sidecar and a.sidecar.mb_match_candidate
        )
        aid = nc.id
    else:
        aid = m.group(1)
    r = demo_client.post(f"/confirm/{aid}")
    assert r.status_code == 200, r.text
    assert "Tagged" in r.text
    # Album should now be in DONE state (hidden from inbox)
    tasks_after = demo_client.get("/tasks").text
    assert "Gimme Some Money" not in tasks_after


def test_demo_auto_resets_on_version_change(music_dir, monkeypatch):
    """A demo dir seeded against an older dataset should auto-reset."""
    demo.seed(music_dir)
    assert demo.is_demo_dir(music_dir)
    # Drop a fake older-version marker
    (music_dir / demo.DEMO_MARKER).write_text(
        "Harmonist demo data — safe to delete.\nversion: deadbeefcafe\n"
    )
    # Add a stray that should be wiped on reset
    (music_dir / "stray_artist").mkdir()
    (music_dir / "stray_artist" / "stale.txt").write_text("from old demo")

    demo.ensure_seeded(music_dir)

    # Stale file gone, marker updated to current version
    assert not (music_dir / "stray_artist").exists()
    assert demo._marker_version(music_dir) == demo.data_version()


def test_demo_no_reset_when_version_matches(music_dir):
    demo.seed(music_dir)
    # Plant a marker that should NOT trigger a reset
    (music_dir / "Wyld Stallion" / "user_added.txt").write_text("kept")
    demo.ensure_seeded(music_dir)
    assert (music_dir / "Wyld Stallion" / "user_added.txt").exists()


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
    client = TestClient(create_app(cfg), headers={"HX-Request": "true"})
    r = client.post("/demo/reset")
    assert r.status_code == 404
    # No banner
    home = client.get("/")
    assert "Demo Mode" not in home.text
