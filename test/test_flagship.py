"""Flagship E2E test — drives the full sync → reconcile → tag pipeline.

Uses demo mode to make external services deterministic. Exercises:

  1. POST /sync starts the background runner.
  2. Polls /sync/status until state returns to idle.
  3. New album appears in /tasks at Needs MBID.
  4. POST /recheck/{id} → lookup_by_bandcamp_url + fetch_release + assess +
     auto-tag (because demo data is configured for exact match).
  5. Album transitions to Done; file tags include the full Picard MBID atom
     set; sidecar persists `mb_release_id` + `tagged_at`.

This is the test that asserts "the headline workflow works end-to-end". A
regression here means a user clicking Sync would land in a broken state.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mutagen.mp4 import MP4

from harmonist import demo, scanner
from harmonist.config import (
    BandcampConfig,
    Config,
    PathsConfig,
    ServerConfig,
    TestConfig,
)
from harmonist.models import AlbumState
from harmonist.tagger import ATOM_MB_ALBUM_ID, ATOM_MB_RELEASE_GROUP_ID, ATOM_MB_TRACK_ID
from harmonist.web.main import create_app


@pytest.fixture(autouse=True)
def reset_demo_state():
    """Restore monkey-patches between tests so demo doesn't leak."""
    from harmonist import cover_art, mb_lookup, mb_search

    saved = (
        mb_lookup.fetch_release,
        mb_lookup.fetch_release_urls,
        mb_lookup.lookup_by_bandcamp_url,
        mb_search.search_releases,
        cover_art.ensure_cover,
    )
    demo.pending_downloads.reset()
    yield
    demo.pending_downloads.reset()
    (
        mb_lookup.fetch_release,
        mb_lookup.fetch_release_urls,
        mb_lookup.lookup_by_bandcamp_url,
        mb_search.search_releases,
        cover_art.ensure_cover,
    ) = saved


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


def _wait_for_idle(client, timeout: float = 5.0) -> dict:
    """Poll /sync/status until state == idle. Returns the final status dict."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get("/sync/status").json()
        if status["state"] == "idle":
            return status
        time.sleep(0.05)
    raise AssertionError(f"sync did not return to idle within {timeout}s")


def _album_by_title(music_dir: Path, title: str):
    for a in scanner.scan(music_dir):
        if a.title == title:
            return a
    raise AssertionError(f"no album titled {title!r} in {music_dir}")


def test_flagship_sync_then_recheck_then_done(demo_client, tmp_path):
    """The headline path: Sync, Recheck, file ends Done with full MB tags."""
    music_dir = tmp_path / "music"

    # --- 1. Click Sync (force full/download mode via the popover) ---
    r = demo_client.post("/sync", data={"from_popover": "true"})
    assert r.status_code == 200
    _wait_for_idle(demo_client)

    # A full sync fetches the owned-but-not-on-disk purchases; CB4 lands new.
    new_album = _album_by_title(music_dir, "Straight Outta Lowcash")
    assert new_album.state == AlbumState.NEEDS_MBID
    assert new_album.sidecar.store_url == "https://cb4.bandcamp.com/album/straight-outta-lowcash"
    assert new_album.sidecar.mb_release_id is None

    # --- 2. Click Recheck on the new album ---
    r = demo_client.post(f"/recheck/{new_album.id}")
    assert r.status_code == 200, r.text
    # With exact match (deltas == 0 in demo data), recheck auto-tags
    assert "tagged" in r.text.lower()

    # --- 3. Verify final state ---
    final = _album_by_title(music_dir, "Straight Outta Lowcash")
    assert final.state == AlbumState.COMPLETE
    assert final.sidecar.mb_release_id == "demo-rel-cb4"
    assert final.sidecar.tagged_at is not None
    # Bandcamp block preserved through tagging
    assert final.sidecar.store_url == "https://cb4.bandcamp.com/album/straight-outta-lowcash"
    assert final.sidecar.bandcamp.item_id == 2001

    # --- 4. File tags actually written ---
    first_track = sorted(new_album.path.glob("*.m4a"))[0]
    audio = MP4(first_track)
    assert ATOM_MB_ALBUM_ID in audio
    assert audio[ATOM_MB_ALBUM_ID][0].decode("utf-8") == "demo-rel-cb4"
    assert ATOM_MB_RELEASE_GROUP_ID in audio
    assert ATOM_MB_TRACK_ID in audio


def test_flagship_sync_status_visible_during_run(demo_client):
    """Confirms /sync/status returns running state while sync is in flight.

    We can't easily race the thread reliably in a test, so instead we verify
    the *shape* of the running state once the runner has started.
    """
    runner = demo_client.app.state.sync_runner
    # Manually flip to running so we can inspect the status response shape
    runner._status.state = "running"
    r = demo_client.get("/sync/status")
    body = r.json()
    assert body["state"] == "running"
    runner._status.state = "idle"


def test_flagship_full_sync_downloads_then_second_is_noop(demo_client, tmp_path):
    """A full sync fetches every owned-but-not-on-disk purchase; a second full
    sync has nothing left to download."""
    music_dir = tmp_path / "music"

    demo_client.post("/sync", data={"from_popover": "true"})
    _wait_for_idle(demo_client)
    after_first = {a.title for a in scanner.scan(music_dir)}
    assert "Straight Outta Lowcash" in after_first  # CB4
    assert "Nagelbett" in after_first  # Autobahn
    count_after_first = len(after_first)

    # One more full sync — everything is now on disk / linked, so it's a no-op.
    demo_client.post("/sync", data={"from_popover": "true"})
    _wait_for_idle(demo_client)
    assert len(scanner.scan(music_dir)) == count_after_first


def test_flagship_409_when_sync_in_flight(demo_client):
    runner = demo_client.app.state.sync_runner
    runner._status.state = "running"
    try:
        r = demo_client.post("/sync")
        assert r.status_code == 409
    finally:
        runner._status.state = "idle"


def test_flagship_new_to_done_via_reconcile_and_confirm(demo_client, tmp_path):
    """Alt path: New → Reconcile → Needs Sync (already tagged)."""
    music_dir = tmp_path / "music"
    new_album = _album_by_title(music_dir, "A Most Excellent Journey")
    assert new_album.state == AlbumState.NEW

    # Reconcile: writes a sidecar derived from MBID tag + ©cmt
    r = demo_client.post(f"/reconcile/{new_album.id}")
    assert r.status_code == 200, r.text

    after_reconcile = _album_by_title(music_dir, "A Most Excellent Journey")
    # Reconcile writes a sidecar with store_url + mb_release_id set (from
    # the tag). Files are already MBID-tagged, so the matching-MBID check
    # passes → state goes straight to NEEDS_SYNC (item_id is None).
    assert after_reconcile.state == AlbumState.NEEDS_SYNC
    assert after_reconcile.sidecar.mb_release_id == "demo-rel-wyld"
    assert (
        after_reconcile.sidecar.store_url
        == "https://wyldstallion.bandcamp.com/album/a-most-excellent-journey"
    )


def test_flagship_mistag_surfaces_after_first_sync(demo_client, tmp_path):
    """Realistic flow: Fever Dog starts NEEDS_SYNC (tagged as the standard edition);
    the first sync's post-sync mis-tag detection browses the release group, spots
    the owned live edition, and demotes it to a mis-tag — surfacing AFTER the sync,
    not pre-seeded."""
    music_dir = tmp_path / "music"
    assert _album_by_title(music_dir, "Fever Dog").state == AlbumState.NEEDS_SYNC

    demo_client.post("/sync")
    _wait_for_idle(demo_client)

    after = _album_by_title(music_dir, "Fever Dog")
    assert after.state == AlbumState.NEEDS_MBID
    cand = after.sidecar.mb_match_candidate
    assert cand is not None
    assert cand.mistag_owned_url == "https://stillwater.bandcamp.com/album/fever-dog-live"
    assert cand.mb_release_id == "demo-rel-fever-live"  # re-tag target = the live edition
