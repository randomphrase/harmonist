from __future__ import annotations

import asyncio
import shutil
import threading
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from mutagen.mp4 import MP4

from harmonist import formats, library_index, live_counts, scanner
from harmonist.web.dir_watcher import watch_music_dir
from harmonist.web.scan_runner import ScanRunner
from test.test_auto_apply import _cfg, _flagged, _no_musicbrainz
from test.test_gardener import _release
from test.test_scan_runner import _album
from test.test_web import _confirmation_fields, _make_tagged_album, _release_for_match, cfg, client

__all__ = ["cfg", "client"]


async def _finish(runner):
    task = runner._task
    assert task is not None
    await task


def test_retag_refreshes_only_its_album(client, cfg, monkeypatch):
    album_dir = _make_tagged_album(
        cfg, "Retag", mbid="rel-1", tagged_at=datetime.now(UTC), item_id=1
    )
    _make_tagged_album(cfg, "Other", mbid="rel-2", tagged_at=datetime.now(UTC), item_id=2)
    runner = client.app.state.scan_runner
    runner.refresh_now()
    album = next(album for album in runner.albums() if album.path == album_dir)
    live_counts.reset_from(runner.albums())
    monkeypatch.setattr(runner, "is_engaged", lambda: True)
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release", lambda mbid: _release_for_match(mbid, n_tracks=1)
    )
    monkeypatch.setattr("harmonist.cover_art.front_image", lambda *args, **kwargs: None)
    walks = []
    requested = []
    original = scanner.iter_album_dirs

    def walk(root, **kwargs):
        walks.append(root)
        yield from original(root, **kwargs)

    monkeypatch.setattr(scanner, "iter_album_dirs", walk)
    monkeypatch.setattr(runner, "request_scan", lambda: requested.append(True))
    response = client.post(f"/retag/{album.id}")

    assert "album-retagged" in response.headers.get("HX-Trigger", "")
    current = next(album for album in runner.albums() if album.path == album_dir)
    assert current.title == "Title"
    assert live_counts.to_status()["library"] == 2
    assert walks == []
    assert requested == []


def _title(album_dir, title):
    track = album_dir / "01 Track.m4a"
    audio = MP4(track)
    audio["\u00a9alb"] = [title]
    audio.save()
    return track


def test_watcher_refreshes_known_album_and_preserves_later_external_edits(tmp_path, monkeypatch):
    album_dir = _album(tmp_path, "One")
    _album(tmp_path, "Other")
    runner = ScanRunner(tmp_path)
    walks = []
    original_walk = scanner.iter_album_dirs
    reads = []
    original_read = formats.read_scan_fields

    def walk(root, **kwargs):
        walks.append(root)
        yield from original_walk(root, **kwargs)

    def read(path):
        reads.append(path)
        return original_read(path)

    async def flush(paths):
        runner.request_refresh(paths)
        await asyncio.sleep(0)
        await _finish(runner)

    async def run():
        runner.attach_loop()
        await _finish(runner)
        monkeypatch.setattr(scanner, "iter_album_dirs", walk)
        monkeypatch.setattr(formats, "read_scan_fields", read)
        track = _title(album_dir, "Written by Harmonist")
        assert runner.refresh_album([album_dir], notify=True)
        seq = runner.status()["seq"]
        reads.clear()
        await flush({track})
        await flush({track})
        assert runner.status()["seq"] == seq
        assert reads == []

        _title(album_dir, "Edited in Picard")
        await flush({track})
        current = next(album for album in runner.albums() if album.path == album_dir)
        assert current.title == "Edited in Picard"
        assert reads == [track]
        assert runner.status()["seq"] == seq + 1
        assert runner.status()["state"] == "done"
        assert walks == []

        new_dir = _album(tmp_path, "New")
        await flush({new_dir, new_dir / "01 Track.m4a"})
        assert len(walks) == 1
        assert len(runner.albums()) == 3
        shutil.rmtree(new_dir)
        await flush({new_dir})
        assert len(walks) == 2
        assert len(runner.albums()) == 2

    asyncio.run(run())


@pytest.mark.parametrize("partial_failure", [False, True])
def test_automatic_tagging_refreshes_without_watcher(tmp_path, monkeypatch, partial_failure):
    from harmonist import auto_apply, gardener, retag
    from harmonist.tagger import PicardCompatibleTagger
    from harmonist.web.main import _auto_apply_if_idle

    auto_apply.reset()
    release = _release("Updated title")
    original_album = _flagged(tmp_path, release)
    _no_musicbrainz(monkeypatch)
    runner = ScanRunner(tmp_path)
    runner.refresh_now()
    album = next(album for album in runner.albums() if album.path == original_album.path)
    gardener.refresh_flag(album, release)
    runner.set_on_rebuilt(
        lambda albums: [gardener.refresh_flag(rebuilt, release) for rebuilt in albums]
    )
    original_tag = retag.tag_with_release

    def write_then_fail(*args, **kwargs):
        original_tag(*args, **kwargs)
        raise OSError("failed after writing tags")

    if partial_failure:
        monkeypatch.setattr(retag, "tag_with_release", write_then_fail)
    app = Mock(
        state=SimpleNamespace(
            cfg=_cfg(tmp_path, apply_up_to="identity"), tagger=PicardCompatibleTagger()
        )
    )
    idle = Mock(is_running=False)
    assert _auto_apply_if_idle(app, idle, idle, runner) is None
    workers = [thread for thread in threading.enumerate() if thread.name == "harmonist-auto-apply"]
    for worker in workers:
        worker.join(5)
        assert not worker.is_alive()
    current = next(album for album in runner.albums() if album.path == original_album.path)
    assert current.title == "Updated title"
    assert auto_apply.is_held(current) == partial_failure
    auto_apply.reset()


def test_confirmation_updates_state_and_counts_without_full_scan(client, cfg, monkeypatch):
    from test.helpers import write_track_totals

    album_dir = _make_tagged_album(cfg, "Incomplete", mbid="rel-1", tagged_at=datetime.now(UTC))
    write_track_totals(album_dir, track_total=2)
    runner = client.app.state.scan_runner
    runner.refresh_now()
    live_counts.reset_from(runner.albums())
    assert live_counts.to_status()["incomplete"] == 1
    monkeypatch.setattr(runner, "is_engaged", lambda: True)
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release", lambda mbid: _release_for_match(mbid, n_tracks=1)
    )
    monkeypatch.setattr("harmonist.cover_art.front_image", lambda *args, **kwargs: None)
    album_id = runner.albums()[0].id
    client.post(f"/retag/{album_id}")
    editor = client.get(f"/assignments/{album_id}?album_tracks=true&on_album_page=true")
    fields = _confirmation_fields(editor.text)
    walks = []
    requests = []
    original = scanner.iter_album_dirs

    def walk(root, **kwargs):
        walks.append(root)
        yield from original(root, **kwargs)

    monkeypatch.setattr(scanner, "iter_album_dirs", walk)
    monkeypatch.setattr(runner, "request_scan", lambda: requests.append(True))
    response = client.post(f"/confirm/{album_id}/accept", data=fields)
    assert "confirmation-applied" in response.headers.get("HX-Trigger", "")
    assert runner.albums()[0].expected_track_count == 1
    assert live_counts.to_status()["incomplete"] == 0
    assert live_counts.to_status()["complete"] == 1
    assert walks == []
    assert requests == []


def test_targeted_refresh_preserves_multi_folder_identity(client, cfg):
    first = _make_tagged_album(
        cfg, "Part One", mbid="rel-1", tagged_at=datetime.now(UTC), release_track_id="rt-1"
    )
    second = _make_tagged_album(
        cfg, "Part Two", mbid="rel-1", tagged_at=datetime.now(UTC), release_track_id="rt-2"
    )
    runner = client.app.state.scan_runner
    runner.refresh_now()
    assert len(runner.albums()) == 1
    assert set(runner.albums()[0].folders) == {first, second}
    for folder in (first, second):
        _title(folder, "Updated album")
    assert runner.refresh_album([first, second], notify=True)
    assert len(runner.albums()) == 1
    assert runner.albums()[0].title == "Updated album"
    assert {entry.album.title for entry in runner._scanned} == {"Updated album"}
    assert runner.albums()[0].track_count == 2
    assert live_counts.to_status()["library"] == 1


def test_targeted_refresh_updates_counts_indexes_and_is_idempotent(client, cfg):
    album_dir = _make_tagged_album(
        cfg, "Tagged", mbid="rel-1", tagged_at=datetime.now(UTC), item_id=123
    )
    runner = client.app.state.scan_runner
    runner.refresh_now()
    live_counts.reset_from(runner.albums())
    library_index.reset_from(runner.albums())
    assert live_counts.to_status()["library"] == 1
    assert library_index.item_ids() == {123}
    (album_dir / "01 Track.m4a").unlink()
    assert runner.refresh_album([album_dir], notify=True)
    assert runner.albums() == []
    assert live_counts.to_status()["library"] == 0
    assert library_index.item_ids() == set()
    seq = runner.status()["seq"]
    assert not runner.refresh_album([album_dir], notify=True)
    assert runner.status()["seq"] == seq


def test_unreadable_watcher_refresh_requests_full_scan(tmp_path, monkeypatch, caplog):
    album_dir = _album(tmp_path, "One")
    runner = ScanRunner(tmp_path)
    runner.refresh_now()

    def unreadable(*args):
        raise OSError("stale mount")

    monkeypatch.setattr(scanner, "read_dir", unreadable)
    assert not runner._refresh_changed({album_dir / "01 Track.m4a"})
    assert len(runner.albums()) == 1
    assert "stale mount" in caplog.text


def test_full_scan_cannot_overwrite_a_more_recent_album_refresh(tmp_path, monkeypatch):
    album_dir = _album(tmp_path, "One")
    other = _album(tmp_path, "Other")
    runner = ScanRunner(tmp_path)
    waiting = threading.Event()
    resume = threading.Event()
    original = scanner.resolve_dir
    original_walk = scanner.iter_album_dirs

    def ordered_walk(root, **kwargs):
        yield from sorted(original_walk(root, **kwargs), key=lambda item: item[0])

    def resolve(path, *args):
        if path == other:
            waiting.set()
            assert resume.wait(5)
        return original(path, *args)

    async def run():
        runner.attach_loop()
        await _finish(runner)
        monkeypatch.setattr(scanner, "resolve_dir", resolve)
        monkeypatch.setattr(scanner, "iter_album_dirs", ordered_walk)
        runner._kick_quiet()
        try:
            assert await asyncio.to_thread(waiting.wait, 5)
            _title(album_dir, "New tags")
            runner.refresh_album([album_dir], notify=True)
        finally:
            resume.set()
        await _finish(runner)
        current = next(album for album in runner.albums() if album.path == album_dir)
        assert current.title == "New tags"

    asyncio.run(run())


def test_real_watcher_delivers_paths_to_targeted_refresh(tmp_path, monkeypatch):
    from harmonist import sidecar
    from harmonist.models import Sidecar

    album_dir = _album(tmp_path, "One")
    runner = ScanRunner(tmp_path)
    walks = []
    original = scanner.iter_album_dirs

    def walk(root, **kwargs):
        walks.append(root)
        yield from original(root, **kwargs)

    async def run():
        runner.attach_loop()
        await _finish(runner)
        monkeypatch.setattr(scanner, "iter_album_dirs", walk)
        stop = asyncio.Event()
        events = []

        def changed(paths):
            events.append(paths)
            runner.request_refresh(paths)

        watcher = asyncio.create_task(
            watch_music_dir(tmp_path, changed, settle=timedelta(seconds=0.1), stop_event=stop)
        )
        try:
            await asyncio.sleep(0.3)
            _title(album_dir, "External update")
            async with asyncio.timeout(5):
                while runner.albums()[0].title != "External update":
                    await asyncio.sleep(0.01)
            # macOS FSEvents replays paths created shortly before a watch starts,
            # so this first batch can carry the album directory itself, which
            # rightly costs a walk. Only Harmonist's own write below must not;
            # an external edit's targeted refresh is the injected-path test's.
            walks.clear()
            events.clear()
            sidecar.write(album_dir, Sidecar(temp_uid="local-album"))
            runner.refresh_album([album_dir], notify=True)
            seq = runner.status()["seq"]
            async with asyncio.timeout(5):
                while not events:
                    await asyncio.sleep(0.01)
            await _finish(runner)
            assert runner.status()["seq"] == seq
            assert walks == []
        finally:
            stop.set()
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher

    asyncio.run(run())


def test_watcher_combines_paths_and_ignores_demo_database(tmp_path, monkeypatch):
    from watchfiles import Change

    from harmonist.web import dir_watcher

    first = tmp_path / "first.m4a"
    second = tmp_path / "second.m4a"
    seen = []

    async def run():
        delivered = asyncio.Event()

        async def events(*args, **kwargs):
            yield {
                (Change.modified, str(first)),
                (Change.modified, str(tmp_path / ".demo-activity.db-wal")),
            }
            await asyncio.sleep(0.01)
            yield {(Change.added, str(second))}
            await asyncio.Event().wait()

        def changed(paths):
            seen.append(paths)
            delivered.set()

        monkeypatch.setattr(dir_watcher, "awatch", events)
        watcher = asyncio.create_task(
            watch_music_dir(tmp_path, changed, settle=timedelta(seconds=0.05))
        )
        try:
            await asyncio.wait_for(delivered.wait(), 2)
            assert seen == [{first, second}]
        finally:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher

    asyncio.run(run())
