"""The public demo's ordinary workflows, distinct from recovery fixtures."""

from __future__ import annotations

import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from harmonist import activity, activity_store, cover_art, demo, formats, gardener, scanner, tagger
from harmonist.config import BandcampConfig, Config, PathsConfig
from harmonist.formats.owned import Significance
from harmonist.models import AlbumState
from harmonist.web.main import create_app


@pytest.fixture
def library(tmp_path):
    demo.install()
    demo.seed(tmp_path)
    return tmp_path


def albums(root):
    return {a.title: a for a in scanner.scan(root)}


def audio_files(root):
    return sorted(p for p in root.rglob("*") if p.is_file() and formats.is_supported(p))


def test_library_has_focused_changes_and_quiet_controls(library):
    current = albums(library)
    assert len(current) == 12
    expected = {
        "We Are Here To Make You Sad": Significance.COSMETIC,
        "Rawhide": Significance.ENRICHMENT,
        "Fever Dog": Significance.STRUCTURE,
        "Man of Constant Sorrow": Significance.IDENTITY,
        "The Rural Juror (OST)": Significance.IDENTITY,
    }
    for name, album in current.items():
        if album.state == AlbumState.NEEDS_MBID:
            continue
        if name == "Fever Dog":
            with pytest.raises(tagger.TrackAssignmentRequired):
                gardener.plan_for(album, demo.fetch_release(album.sidecar.mb_release_id))
            continue
        plan = gardener.plan_for(album, demo.fetch_release(album.sidecar.mb_release_id))
        assert gardener.verdict_for(plan) == expected.get(name), name
    assert len(current["Can You Picture That?"].paths) == 2
    assert current["Can You Picture That?"].state == AlbumState.COMPLETE
    assert {p.suffix for p in audio_files(library)} == {".mp3", ".m4a", ".flac"}
    assert {a.title for a in current.values() if a.state == AlbumState.NEEDS_MBID} == {
        "Gimme Some Money",
        "Top 5 Records For A Wednesday",
    }


def test_adoption_recipe_preserves_tags_and_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("HARMONIST_DEMO_ADOPTION", "1")
    demo.seed(tmp_path)
    assert not list(tmp_path.rglob(".harmonist.json"))
    current = albums(tmp_path)
    assert current["After Hours"].state == AlbumState.NEW
    assert (
        len(
            list(
                (tmp_path / "Dr. Teeth and the Electric Mayhem" / "Can You Picture That?").glob(
                    "CD*"
                )
            )
        )
        == 2
    )
    assert not activity.recent()
    assert formats.read_album_id(audio_files(tmp_path / "Barry Jive and the Uptown Five")[0])
    assert not formats.read_album_id(audio_files(tmp_path / "Sonic Death Monkey")[0])


def test_seeded_history_is_real_and_survives_restart(tmp_path):
    demo.install()
    demo.ensure_seeded(tmp_path, persistent_history=True)
    assert len([e for e in activity.recent() if e.message == "Updated tags from MusicBrainz"]) == 2
    ids = [r[0] for r in activity_store._ensure().execute("SELECT event_id FROM tag_changes")]
    detail = activity_store.tag_changes_for(ids)
    assert detail
    assert all("date" in record.changes for record in detail.values())
    before = len(activity.recent())
    activity_store.init_memory()
    demo.ensure_seeded(tmp_path, persistent_history=True)
    assert len(activity.recent()) == before
    demo.reset(tmp_path, persistent_history=True)
    assert len(activity.recent()) == before


def test_startup_adopts_both_discs_without_rewriting_audio(tmp_path, monkeypatch):
    monkeypatch.setenv("HARMONIST_DEMO_ADOPTION", "1")
    root = tmp_path / "music"
    cfg = Config(paths=PathsConfig(music_dir=root, config_dir=tmp_path / "cfg"), demo_mode=True)
    app = create_app(cfg)
    before = {p.relative_to(root): p.read_bytes() for p in audio_files(root)}
    with TestClient(app):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (
                any(e.message.startswith("Reconcile done:") for e in activity.recent())
                and not app.state.reconcile_runner.is_running
            ):
                break
            time.sleep(0.02)
        else:
            pytest.fail("startup adoption did not finish")
        current = albums(root)
        assert len(current) == 12
        assert len(current["Can You Picture That?"].paths) == 2
        assert {p.relative_to(root): p.read_bytes() for p in audio_files(root)} == before


def test_preview_apply_undo_preserves_personal_tags(library):
    from harmonist import tag_history

    album = albums(library)["After Hours"]
    files = audio_files(album.path)
    before = {p.name: formats.read_owned(p) for p in files}
    records = list(
        activity_store.tag_changes_for(
            [r[0] for r in activity_store._ensure().execute("SELECT event_id FROM tag_changes")]
        ).values()
    )
    # Only the second seeded update belongs to this album.
    tagger.revert_tags(album.path, tag_history.revert_plan(records[-3:]))
    assert all(formats.read_tags(p).date == "2024" for p in files)
    plan = gardener.plan_for(album, demo.fetch_release(album.id))
    assert {k for cs in plan.changes.values() for k in cs} == {"date"}
    tagger.tag_album(album.path, demo.fetch_release(album.id))
    assert {p.name: formats.read_owned(p) for p in files} == before
    assert all("keep my notes" in (formats.read_comment(p) or "") for p in files)
    from mutagen.flac import FLAC

    assert all(FLAC(p)["genre"] == ["Personal favourites"] for p in files)


def test_artwork_check_is_offline_and_supplies_real_larger_candidate(library, tmp_path):
    cover_art.configure_cache(tmp_path / "caa")
    result = cover_art.check_front("demo-rel-mouserat", keep_if_wider_than=320)
    assert (result.width, result.height) == (960, 960)
    candidate = cover_art.cached_image("demo-rel-mouserat")
    assert (
        candidate and candidate.read_bytes() == (demo.ASSETS_DIR / "mouse-archive.png").read_bytes()
    )
    album = albums(library)["The Awesome Album"]
    from harmonist import artwork_store

    artwork_store.configure(tmp_path / "kept")
    before = [formats.read_cover(p) for p in audio_files(album.path)]
    files = audio_files(album.path)
    front = cover_art.cached_front(album.id)
    plan = tagger.decide_artwork(album.path, files, album.cover_path, archive=front)
    tagger.apply_artwork(album.path, plan, files=files, cover_path=album.cover_path, archive=front)
    assert (album.path / "cover.png").read_bytes() == candidate.read_bytes()
    # A larger folder cover is a legitimate layout, not permission to replace
    # every embedded picture. The default upgrade changes only the folder.
    assert before == [formats.read_cover(p) for p in files]
    from harmonist import tag_history

    events = activity_store.album_history(album.id)
    records = activity_store.tag_changes_for([event.id for event in events])
    tagger.restore_artwork(album.path, tag_history.artwork_revert_plan(list(records.values())))
    assert (album.path / "cover.png").read_bytes() == (demo.ASSETS_DIR / "mouse.png").read_bytes()
    assert [formats.read_cover(p) for p in audio_files(album.path)] == before


@pytest.fixture
def client(tmp_path):
    root = tmp_path / "music"
    cfg = Config(
        paths=PathsConfig(music_dir=root, config_dir=tmp_path / "cfg"),
        bandcamp=BandcampConfig(download_format="flac", ignores_file=root / "ignores.txt"),
        demo_mode=True,
    )
    return TestClient(create_app(cfg), headers={"HX-Request": "true"})


def wait_sync(client):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = client.get("/sync/status").json()
        if state["state"] == "idle":
            assert not state.get("last_error"), state
            return
        time.sleep(0.02)
    pytest.fail("sync did not finish")


def test_seeded_suggestion_is_reviewable_after_seed_reset_and_restart(client, monkeypatch):
    from bs4 import BeautifulSoup

    from harmonist import mb_lookup

    monkeypatch.setattr(
        mb_lookup, "fetch_release", lambda *a, **k: pytest.fail("display fetched MusicBrainz")
    )
    root = client.app.state.cfg.paths.music_dir
    for step in ("seed", "reset", "restart"):
        if step == "reset":
            demo.reset(root, persistent_history=True)
        elif step == "restart":
            activity_store.init_memory()
            demo.ensure_seeded(root, persistent_history=True)
        album = albums(root)["Gimme Some Money"]
        response = client.get(f"/assignments/{album.id}?cancel=true&on_album_page=true")
        assert response.status_code == 200
        panel = BeautifulSoup(response.text, "html.parser")
        assert len(panel.select("[data-assignment-row]")) == 3, step
        assert panel.select_one('button[hx-post^="/confirm/"]') is not None


def test_link_existing_download_new_and_upgrade_mp3_to_flac(client):
    root = client.app.state.cfg.paths.music_dir
    original = {p.relative_to(root): p.read_bytes() for p in audio_files(root)}
    assert client.post("/sync").status_code == 200
    wait_sync(client)
    assert {p.relative_to(root): p.read_bytes() for p in audio_files(root)} == original
    assert albums(root)["A Most Excellent Journey"].state == AlbumState.COMPLETE
    assert (
        client.post("/pending/2003/match", data={"album_id": "demo-rel-mouserat"}).status_code
        == 200
    )
    assert albums(root)["The Awesome Album"].sidecar.bandcamp.item_id == 2003
    assert client.post("/pending/2001/download").status_code == 200
    assert client.post("/sync").status_code == 200
    wait_sync(client)
    cb4 = albums(root)["Straight Outta Lowcash"]
    assert cb4.state == AlbumState.COMPLETE
    assert {p.suffix for p in audio_files(cb4.path)} == {".flac"}
    assert client.post("/library/demo-rel-mouserat/redownload").status_code == 200
    wait_sync(client)
    mouse = albums(root)["The Awesome Album"]
    assert mouse.state == AlbumState.COMPLETE
    assert {p.suffix for p in audio_files(mouse.path)} == {".flac"}
    archives = list(root.glob("*.zip"))
    assert len(archives) == 1
    with zipfile.ZipFile(archives[0]) as archive:
        mp3s = [n for n in archive.namelist() if n.endswith(".mp3")]
        assert len(mp3s) == 3
        for name in mp3s:
            assert archive.read(name) == next(
                data for path, data in original.items() if str(path) == name
            )
    assert not list(mouse.path.glob("*.mp3"))
    assert client.post("/demo/reset").status_code == 200
    assert not list(root.glob("*.zip"))
    assert {p.suffix for p in audio_files(albums(root)["The Awesome Album"].path)} == {".mp3"}
    assert not (root / "ignores.txt").exists()
    assert not demo.pending_downloads.is_approved(2001)
    assert not demo.pending_downloads.is_approved(2003)
    # A second recording starts with the same link-only decisions, not stale
    # approvals silently downloading the albums approved in the first take.
    assert client.post("/sync").status_code == 200
    wait_sync(client)
    assert len(albums(root)) == 12
    assert demo.pending_downloads.count() == 3


def test_forced_full_sync_does_not_overwrite_an_unlinked_copy(library):
    mouse = albums(library)["The Awesome Album"]
    before = {p.name: p.read_bytes() for p in audio_files(mouse.path)}
    demo.run_demo_sync(library, download_format="flac")
    assert {p.name: p.read_bytes() for p in audio_files(mouse.path)} == before
    assert demo.pending_downloads.get(2003) is not None


def test_download_tagging_has_one_undo_per_album(client):
    from harmonist import tag_history

    root = client.app.state.cfg.paths.music_dir
    client.post("/sync")
    wait_sync(client)
    for item_id in (2001, 2002):
        assert client.post(f"/pending/{item_id}/download").status_code == 200
    client.post("/sync")
    wait_sync(client)

    anchors = {}
    action_ids = set()
    for album_id in ("demo-rel-cb4", "demo-rel-autobahn"):
        events = activity_store.album_history(album_id)
        detail = activity_store.tag_changes_for([event.id for event in events])
        groups = tag_history.group_records(events, detail)
        assert len(groups) == 1
        anchor, records = next(iter(groups.items()))
        album = next(a for a in albums(root).values() if a.id == album_id)
        assert {record.file for record in records} == {p.name for p in audio_files(album.path)}
        event = next(event for event in events if event.id == anchor)
        assert event.message == "Auto-tagged from MusicBrainz after sync"
        assert event.action_id is not None
        action_ids.add(event.action_id)
        anchors[album_id] = anchor
        page = client.get(f"/album/{album_id}")
        assert page.status_code == 200
        assert page.text.count('aria-label="Undo this tagging\'s tag changes"') == 1
    assert len(action_ids) == 2

    cb4 = albums(root)["Straight Outta Lowcash"]
    other = albums(root)["Nagelbett"]
    untouched = {p: formats.read_owned(p) for p in audio_files(other.path)}
    response = client.post("/tags/restore/demo-rel-cb4", data={"event_id": anchors["demo-rel-cb4"]})
    assert response.status_code == 200
    assert all(not formats.read_owned(p)["mb_album_id"] for p in audio_files(cb4.path))
    assert {p: formats.read_owned(p) for p in audio_files(other.path)} == untouched


def test_redownload_also_works_for_an_automatically_linked_album(client):
    root = client.app.state.cfg.paths.music_dir
    client.post("/sync")
    wait_sync(client)
    assert client.post("/library/demo-rel-wyld/redownload").status_code == 200
    wait_sync(client)
    album = albums(root)["A Most Excellent Journey"]
    assert album.state == AlbumState.COMPLETE
    assert album.sidecar.bandcamp.item_id == 1000
    assert {p.suffix for p in audio_files(album.path)} == {".flac"}


def test_download_cap_pauses_then_limits_new_purchases(client):
    root = client.app.state.cfg.paths.music_dir
    client.app.state.cfg.bandcamp.max_downloads_per_sync = 0
    client.post("/sync", data={"from_popover": "true"})
    wait_sync(client)
    assert len(albums(root)) == 12
    assert demo.pending_downloads.count() == 3
    client.app.state.cfg.bandcamp.max_downloads_per_sync = 1
    client.post("/sync", data={"from_popover": "true"})
    wait_sync(client)
    assert len(albums(root)) == 13
    assert albums(root)["Straight Outta Lowcash"].state == AlbumState.COMPLETE
    assert demo.pending_downloads.count() == 2


def test_pacing_only_delays_demo_writes(library, monkeypatch, tmp_path):
    monkeypatch.setenv("HARMONIST_DEMO_DELAY", "0.1")
    sleeps: list[float] = []
    monkeypatch.setattr(demo.time, "sleep", sleeps.append)
    path = audio_files(library / "Sex Bob-omb")[0]
    formats.write_tags(
        path, tagger.tagsets_for(demo.fetch_release("demo-rel-sex-bob-omb"), frozenset())[0], None
    )
    assert sleeps == [0.1]
