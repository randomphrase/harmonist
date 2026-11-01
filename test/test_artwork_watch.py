"""Eligibility decisions feed the same plan as the Library and reviewed writes."""

import json
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from html import unescape
from unittest.mock import Mock

import pytest

from harmonist import (
    activity_store,
    artwork,
    artwork_updates,
    artwork_watch,
    caa_cache,
    cover_art,
    formats,
    mb_cache,
    scanner,
    tagger,
)
from harmonist import sidecar as sc
from harmonist.models import ArtworkWatch, Sidecar
from test import test_web
from test.test_artwork import png_bytes
from test.test_gardener import _release, _stub_app, _stub_runner, _tagged

cfg = test_web.cfg
client = test_web.client


def candidate(image_id):
    return cover_art.Candidate(
        image_id,
        f"https://example.test/{image_id}.png",
        f"https://example.test/{image_id}-250.png",
        ("front",),
        True,
    )


@pytest.fixture
def setup(client, cfg, monkeypatch):
    release = _release()
    monkeypatch.setattr(cover_art, "front_image", lambda *a, **kw: None)
    album = _tagged(cfg.paths.music_dir, release)
    big = png_bytes(3000, 3000)
    (album.path / "cover.png").write_bytes(big)
    for path in album.path.glob("*.m4a"):
        formats.write_cover(path, big)
    album = scanner.scan(cfg.paths.music_dir)[0]
    fetch = Mock(return_value=release)
    monkeypatch.setattr("harmonist.mb_lookup.fetch_release", fetch)
    mb_cache.fetch_release(release["id"])
    listings = {"release": [candidate("200")], "release-group": [candidate("200")]}
    listed = Mock(side_effect=lambda kind, key: list(listings[kind]))
    downloaded = Mock(return_value=(png_bytes(500, 500), "image/png"))
    monkeypatch.setattr(cover_art, "fetch_listing", listed)
    monkeypatch.setattr(cover_art, "fetch_bytes", downloaded)
    caa_cache.configure(timedelta(days=7))
    return album, listings, listed, downloaded, fetch


def start(client, album):
    return client.post(
        f"/album/{album.id}/artwork/watch", data={"release": "rel-aaa", "watch": "true"}
    )


def test_watch_uses_replacement_and_rejection_is_reversible(client, cfg, setup, monkeypatch):
    album, _, listed, downloaded, fetch = setup
    before = {p.name: p.read_bytes() for p in album.path.iterdir() if p.suffix != ".json"}
    assert artwork_updates.operation(album) is None
    response = start(client, album)
    assert response.status_code == 200
    album = scanner.scan(cfg.paths.music_dir)[0]
    assert artwork_updates.operation(album) == artwork.Operation.REPLACEMENT
    assert listed.call_count == 2 and downloaded.call_count == 1 and fetch.call_count == 1
    stamp = sc.sidecar_path(album.path).stat().st_mtime_ns
    start(client, album)
    assert sc.sidecar_path(album.path).stat().st_mtime_ns == stamp
    assert listed.call_count == 2 and downloaded.call_count == 1
    client.post(f"/album/{album.id}/artwork/update", data={"plan": "stale"})
    assert sc.read(album.path).artwork_watch == ArtworkWatch("rel-aaa")
    # Library counts are derived entirely from the warmed descriptions.
    with monkeypatch.context() as isolated:
        isolated.setattr(caa_cache, "stored_listing", Mock(side_effect=AssertionError("disk")))
        assert artwork_updates.operation(album) == artwork.Operation.REPLACEMENT
    url = f"/album/{album.id}/artwork/suggestion/200"
    assert client.post(url, data={"rejected": "true"}).status_code == 200
    album = scanner.scan(cfg.paths.music_dir)[0]
    assert artwork_updates.operation(album) is None
    assert sc.read(album.path).artwork_rejected == ("200",)
    stamp = sc.sidecar_path(album.path).stat().st_mtime_ns
    client.post(url, data={"rejected": "true"})
    assert sc.sidecar_path(album.path).stat().st_mtime_ns == stamp
    artwork_updates.configure(lambda: client.app.state.cfg.tagging.folder_cover)
    artwork_updates.warm([album])
    assert artwork_updates.operation(album) is None
    assert client.post(url, data={"rejected": "false"}).status_code == 200
    album = scanner.scan(cfg.paths.music_dir)[0]
    assert artwork_updates.operation(album) == artwork.Operation.REPLACEMENT
    client.post(f"/album/{album.id}/artwork/watch", data={"release": "rel-aaa"})
    album = scanner.scan(cfg.paths.music_dir)[0]
    assert artwork_updates.operation(album) is None
    assert before == {p.name: p.read_bytes() for p in album.path.iterdir() if p.suffix != ".json"}


def test_artwork_checkboxes_are_not_reported(client, cfg, setup):
    """Better artwork wanted and Don't suggest this image show where they stand,
    so the Activity feed doesn't repeat them (#911). The audit log still has the
    decisions."""
    album, _, _, _, _ = setup
    feed_before = activity_store.recent(50, source=activity_store.Source.ACTIVITY)
    url = f"/album/{album.id}/artwork/suggestion/200"

    responses = [
        start(client, album),
        client.post(url, data={"rejected": "true"}),
        client.post(url, data={"rejected": "false"}),
        client.post(f"/album/{album.id}/artwork/watch", data={"release": "rel-aaa"}),
    ]

    assert [r.status_code for r in responses] == [200, 200, 200, 200]
    assert sc.read(album.path).artwork_watch is None
    assert activity_store.recent(50, source=activity_store.Source.ACTIVITY) == feed_before
    audit = [e.message for e in activity_store.recent(50, source=activity_store.Source.AUDIT)]
    assert len([m for m in audit if "artwork_watch=" in m]) == 2
    assert len([m for m in audit if "artwork_rejected=" in m]) == 2


def test_rejected_default_is_not_suggested_but_manual_use_still_works(client, cfg, setup):
    album, _, _, _, _ = setup
    start(client, album)
    client.post(f"/album/{album.id}/artwork/watch", data={"release": "rel-aaa"})
    client.post(f"/album/{album.id}/artwork/suggestion/200", data={"rejected": "true"})
    album = scanner.scan(cfg.paths.music_dir)[0]
    original = cover_art.cached_candidate_image("200")
    assert original is not None
    # Even a normally-winning default front is suppressed, through both plans.
    cover_art.cache_image("image-200", png_bytes(4000, 4000), "image/png")
    large = cover_art.cached_candidate_image("200")
    assert large is not None
    artwork_updates.observe("rel-aaa")
    files = list(album.path.glob("*.m4a"))
    plan = tagger.decide_artwork(album.path, files, album.cover_path, archive=large)
    assert not plan.changes
    plan = tagger.decide_artwork(
        album.path,
        files,
        album.cover_path,
        archive=large,
        choices={artwork.COVER: "200"},
        picks={"200": large},
    )
    assert plan.operation(artwork.Scope.ALL) == artwork.Operation.REPLACEMENT
    assert (
        client.post(
            f"/album/{album.id}/artwork/suggestion/999", data={"rejected": "true"}
        ).status_code
        == 404
    )


def test_restart_rematch_and_per_track_protection(client, cfg, setup):
    album, _, _, _, _ = setup
    start(client, album)
    album = scanner.scan(cfg.paths.music_dir)[0]
    artwork_updates.configure(lambda: artwork.FolderCoverPolicy.NEVER)
    artwork_updates.warm([album])
    assert artwork_updates.operation(album) == artwork.Operation.REPLACEMENT
    assert album.sidecar is not None
    rematched = replace(album, sidecar=replace(album.sidecar, mb_release_id="another"))
    assert artwork_watch.active(rematched.sidecar) is None
    local = album.artwork_local
    assert local is not None
    tracks = (
        *local.tracks,
        (album.path / "another.m4a", formats.EmbeddedArt.of(png_bytes(600, 600), "image/png")),
    )
    choices, _ = artwork_watch.proposal(
        album.sidecar, tracks, None, artwork.FolderCoverPolicy.NEVER
    )
    assert choices == {}


def test_failed_start_does_not_save_watch(client, setup):
    album, _, listed, _, _ = setup
    listed.side_effect = cover_art.CoverArtError("temporarily unavailable")
    response = start(client, album)
    assert "Couldn't start the artwork watch" in unescape(response.text)
    assert sc.read(album.path).artwork_watch is None


@pytest.mark.parametrize("blocked", ["stale", "unkept", "failed"])
def test_partial_apply_keeps_watch(client, setup, monkeypatch, blocked):
    album, _, _, _, _ = setup
    page = start(client, album).text
    monkeypatch.setattr(
        tagger,
        "apply_artwork",
        Mock(
            return_value=tagger.ArtworkOutcome(
                changed=1, written=("partial",), **{blocked: ("track",)}
            )
        ),
    )
    response = client.post(
        f"/album/{album.id}/artwork/update",
        data={"plan": test_web._form_value(page, "plan"), "use": test_web._carried_use(page)},
    )
    assert response.status_code == 200
    assert sc.read(album.path).artwork_watch == ArtworkWatch("rel-aaa")


def test_reread_discovers_new_alternative_after_rejection(client, cfg, setup, monkeypatch):
    album, listings, _, downloaded, _ = setup
    monkeypatch.setattr(caa_cache, "front", Mock())
    start(client, album)
    client.post(f"/album/{album.id}/artwork/suggestion/200", data={"rejected": "true"})
    listings["release"].append(candidate("300"))
    response = client.get(f"/album/{album.id}/artwork?reread=1")
    assert response.status_code == 200
    assert (
        artwork_updates.operation(scanner.scan(cfg.paths.music_dir)[0])
        == artwork.Operation.REPLACEMENT
    )
    assert downloaded.call_count == 2


def test_round_trip_multifolder_and_independent_decisions(tmp_path):
    from harmonist.models import Album, AlbumState

    folders = [tmp_path / "disc1", tmp_path / "disc2"]
    for folder in folders:
        folder.mkdir()
        sc.write(folder, Sidecar(mb_release_id="rel-aaa"))
        assert "artwork_watch" not in json.loads(sc.sidecar_path(folder).read_text())
        assert "artwork_rejected" not in json.loads(sc.sidecar_path(folder).read_text())
    album = Album(
        id="rel-aaa",
        path=folders[0],
        artist="Artist",
        title="Album",
        track_count=2,
        state=AlbumState.COMPLETE,
        sidecar=sc.read(folders[0]),
        paths=tuple(folders),
    )
    assert artwork_watch.save(album, ArtworkWatch("rel-aaa"))
    assert not artwork_watch.save(album, ArtworkWatch("rel-aaa"))
    assert artwork_watch.reject(album, "200", True)
    assert artwork_watch.reject(album, "300", True)
    assert artwork_watch.reject(album, "200", False)
    assert not artwork_watch.reject(album, "200", False)
    parts = [sc.read(folder) for folder in folders]
    assert all(
        s.artwork_watch == ArtworkWatch("rel-aaa") and s.artwork_rejected == ("300",) for s in parts
    )
    assert scanner._merge_sidecars(parts, "rel-aaa").artwork_rejected == ("300",)
    assert artwork_watch.save(album, None)
    assert all(sc.read(folder).artwork_rejected == ("300",) for folder in folders)


def test_sweep_shares_cache_and_failure_preserves_proposal(client, cfg, setup, monkeypatch):
    album, _, listed, downloaded, _ = setup
    start(client, album)
    album = scanner.scan(cfg.paths.music_dir)[0]
    assert album.sidecar is not None
    other = replace(album, id="other", sidecar=replace(album.sidecar, artwork_watch=None))
    before = {p.name: p.read_bytes() for p in album.path.iterdir() if p.is_file()}
    caa_cache.configure(timedelta(0))
    # The front the listings name, so the check has no listing to ask again (#927).
    front = activity_store.CachedCoverArt(
        fetched_at=datetime.now(UTC), image_url=candidate("200").image_url, source="release"
    )
    monkeypatch.setattr(caa_cache, "front", Mock(return_value=front))
    listed.reset_mock()
    assert artwork_updates.sweep([album, other], limit=10) == 1
    assert listed.call_count == 2 and downloaded.call_count == 1
    assert artwork_updates.operation(album) == artwork.Operation.REPLACEMENT
    assert artwork_updates.operation(other) is None
    assert artwork_updates.sweep([album], limit=10) == 0
    artwork_updates._attempted.clear()
    listed.side_effect = cover_art.CoverArtError("offline")
    assert artwork_updates.sweep([album], limit=10) == 0
    assert artwork_updates.operation(album) == artwork.Operation.REPLACEMENT
    assert artwork_updates.sweep([album], limit=10) == 0
    assert before == {p.name: p.read_bytes() for p in album.path.iterdir() if p.is_file()}


def test_explicit_watch_runs_with_general_background_checks_off(client, cfg, setup, monkeypatch):
    from harmonist.web import main

    album, _, _, _, _ = setup
    start(client, album)
    album = scanner.scan(cfg.paths.music_dir)[0]
    assert album.sidecar is not None
    other = replace(album, id="unwatched", sidecar=replace(album.sidecar, artwork_watch=None))

    class InlineThread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(threading, "Thread", InlineThread)
    sweep = Mock()
    monkeypatch.setattr(artwork_updates, "sweep", sweep)
    scan = _stub_runner()
    scan.albums = lambda: [album, other]
    main._artwork_check_if_idle(_stub_app("off"), _stub_runner(), _stub_runner(), scan)
    sweep.assert_called_once_with([album])


def test_watch_refills_lost_group_evidence_once(client, cfg, setup):
    _, _, listed, _, fetch = setup
    activity_store.init(cfg.paths.config_dir / "cold.db")
    artwork_watch.check("rel-aaa")
    assert fetch.call_count == 2 and listed.call_count == 2
    artwork_watch.check("rel-aaa")
    assert fetch.call_count == 2 and listed.call_count == 2
