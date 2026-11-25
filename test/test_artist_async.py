"""Deterministic ownership, deadline and cancellation boundaries for #1040."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from harmonist import (
    activity_store,
    artist_fill,
    fanart_tv,
    id_registry,
    mb_lookup,
    nfo_history,
    nfo_writer,
    wikimedia,
)
from harmonist.activity_store import Source
from harmonist.async_workers import Workers
from harmonist.web import artist_work
from test.test_artist_fill import ASKING, FOUND, JPEG, LINKED, MBID, MERGED, reading
from test.test_artist_web import artist_client

__all__ = ["artist_client"]


async def wait(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), timeout=5)


async def checkpoint() -> None:
    """Run already-scheduled tasks to their next suspension, without a sleep."""
    loop = asyncio.get_running_loop()
    reached = loop.create_future()
    loop.call_soon(reached.set_result, None)
    await reached


def test_cancelled_sync_lookup_keeps_its_capacity_until_the_thread_finishes():
    async def run():
        loop = asyncio.get_running_loop()
        workers = Workers(1, "test-artist")
        entered = asyncio.Event()
        release = threading.Event()
        observed = []

        def blocked():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "test did not release the sync lookup"
            observed.append("first finished")

        first = asyncio.create_task(workers.run(blocked))
        try:
            await wait(entered)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            second = asyncio.create_task(workers.run(lambda: observed.append("second started")))
            await checkpoint()
            assert not second.done()
            assert observed == []
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            release.set()
            await workers.close()
            assert observed == ["first finished"], "a cancelled queued lookup must never start"
        finally:
            release.set()
            await workers.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "change_identity", [False, True], ids=["picture-appeared", "identity-changed"]
)
def test_a_held_provider_leaves_requests_responsive_and_preserves_intervening_edits(
    artist_client,
    monkeypatch,
    change_identity,
):
    client, cfg, _network = artist_client
    app = client.app
    path = cfg.paths.music_dir / "Carol" / "artist.nfo"
    path.parent.mkdir()
    original = f"<artist><name>Carol</name><musicbrainzartistid>{MBID}</musicbrainzartistid></artist>".encode()
    path.write_bytes(original)
    key = id_registry.get_or_mint(path)
    monkeypatch.setattr(mb_lookup, "fetch_artist", Mock(return_value={"id": MBID, "name": "Carol"}))

    async def run():
        work = artist_work.ArtistWork()
        app.state.artist_work = work
        started, release = asyncio.Event(), asyncio.Event()

        async def picture(*args, **kwargs):
            started.set()
            await wait(release)
            return fanart_tv.Thumb("https://assets.fanart.tv/carol.jpg", "carol")

        monkeypatch.setattr(fanart_tv, "artist_thumb", picture)
        monkeypatch.setattr(artist_fill, "download_picture", AsyncMock(return_value=JPEG))
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1",
                headers={"HX-Request": "true"},
            ) as browser:
                request = asyncio.create_task(
                    browser.post(f"/artist/{MBID}/refresh", data={"file": key})
                )
                await wait(started)
                health = await asyncio.wait_for(browser.get("/healthz"), 5)
                assert health.status_code == 200
                edited = (
                    original.replace(MBID.encode(), MERGED.encode())
                    if change_identity
                    else original
                )
                await asyncio.to_thread(path.write_bytes, edited)
                existing = path.parent / "artist.jpg"
                await asyncio.to_thread(existing.write_bytes, JPEG + b"mine")
                release.set()
                response = await asyncio.wait_for(request, 5)
                assert response.status_code == 200
                assert path.read_bytes() == edited
                assert existing.read_bytes() == JPEG + b"mine"
                if change_identity:
                    assert "file has just changed" in response.text
                assert not [
                    e
                    for e in activity_store.artist_history(MBID)
                    if e.source is Source.AUDIT and e.message.startswith("nfo.write")
                ]
        finally:
            release.set()
            await work.close()

    asyncio.run(run())


def test_two_writers_with_the_same_snapshot_cannot_both_commit(tmp_path, monkeypatch):
    activity_store.init(tmp_path / "activity.db")
    path = tmp_path / "artist.nfo"
    original = b"<artist><name>Original</name></artist>"
    path.write_bytes(original)

    async def run():
        work = artist_work.ArtistWork()
        loop = asyncio.get_running_loop()
        mutating, second_waiting = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        replace = nfo_writer._replace

        def held(file, data):
            loop.call_soon_threadsafe(mutating.set)
            assert release.wait(5)
            replace(file, data)

        class ObservedLock(asyncio.Lock):
            async def acquire(self):
                if self.locked():
                    second_waiting.set()
                return await super().acquire()

        lock = ObservedLock()
        work._paths[path.resolve()] = lock
        monkeypatch.setattr(nfo_writer, "_replace", held)

        def write(name):
            return nfo_history.write(path, root=tmp_path, expected=original, wanted={"name": name})

        try:
            first = asyncio.create_task(work.write(path, lambda: write("First")))
            await wait(mutating)
            second = asyncio.create_task(work.write(path, lambda: write("Second")))
            await wait(second_waiting)
            release.set()
            assert (await asyncio.wait_for(first, 5)).label == "First"
            with pytest.raises(nfo_writer.NfoChangedError):
                await asyncio.wait_for(second, 5)
            assert b"First" in path.read_bytes()
            events = activity_store.artist_history(id_registry.get_or_mint(path))
            assert len([e for e in events if e.message.startswith("nfo.write")]) == 1
        finally:
            release.set()
            await work.close()

    asyncio.run(run())


def test_shutdown_cancels_pending_lookups_and_refuses_new_work():
    async def run():
        work = artist_work.ArtistWork()
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def lookup():
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        pending = asyncio.create_task(work.lookup(lookup))
        await wait(started)
        await work.close()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 1)
        assert cancelled.is_set()
        with pytest.raises(artist_work.Stopping):
            await work.lookup(lookup)

    asyncio.run(run())


def test_identity_timeout_returns_without_waiting_for_the_sync_worker_or_writing(
    artist_client,
    monkeypatch,
):
    client, cfg, _network = artist_client
    app = client.app
    path = cfg.paths.music_dir / "Carol" / "artist.nfo"
    path.parent.mkdir()
    path.write_bytes(b"")
    key = id_registry.get_or_mint(path)

    async def run():
        loop = asyncio.get_running_loop()
        real_time = loop.time
        offset = 0.0
        monkeypatch.setattr(loop, "time", lambda: real_time() + offset)
        work = artist_work.ArtistWork()
        app.state.artist_work = work
        entered = asyncio.Event()
        release = threading.Event()

        def read(mbid):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(10)
            return {"id": mbid, "name": "Carol"}

        monkeypatch.setattr(mb_lookup, "fetch_artist", read)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1",
                headers={"HX-Request": "true"},
            ) as browser:
                request = asyncio.create_task(
                    browser.post(f"/artist/{key}/identify", data={"file": key, "artist": MBID})
                )
                await wait(entered)
                offset = 31.0
                response = await asyncio.wait_for(request, 5)
                assert "30 seconds" in response.text
                assert path.read_bytes() == b""
                release.set()
                await work.close()
                assert path.read_bytes() == b"", "a late MusicBrainz result must never write"
                assert not [
                    e
                    for e in activity_store.artist_history(MBID)
                    if e.message.startswith("nfo.write")
                ]
        finally:
            release.set()
            await work.close()

    asyncio.run(run())


def test_optional_deadline_keeps_a_completed_biography_and_does_not_fall_back(
    monkeypatch, tmp_path
):
    activity_store.init(tmp_path / "activity.db")

    async def run():
        loop = asyncio.get_running_loop()
        real_time = loop.time
        offset = 0.0
        monkeypatch.setattr(loop, "time", lambda: real_time() + offset)
        work = artist_work.ArtistWork()
        started = asyncio.Event()
        biography_finished = asyncio.Event()
        cancelled = asyncio.Event()

        async def hanging(*args, **kwargs):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        async def biography(*args, **kwargs):
            biography_finished.set()
            return FOUND

        commons = AsyncMock(side_effect=AssertionError("timeout must not fall back"))
        monkeypatch.setattr(fanart_tv, "artist_thumb", hanging)
        monkeypatch.setattr(wikimedia, "biography", biography)
        monkeypatch.setattr(wikimedia, "picture", commons)
        try:
            task = asyncio.create_task(
                artist_fill.fill(
                    MBID,
                    lambda: work.musicbrainz.run(reading(LINKED)),
                    lacks_biography=True,
                    lacks_picture=True,
                    asking=ASKING,
                    client=work.http,
                    deadline=loop.time() + 30,
                )
            )
            await wait(started)
            await wait(biography_finished)
            offset = 31.0
            _, result = await asyncio.wait_for(task, 5)
            assert result.biography == FOUND
            assert result.picture is None and result.picture_failed == "fanart.tv"
            assert cancelled.is_set()
            commons.assert_not_awaited()
        finally:
            await work.close()

    asyncio.run(run())


def test_merged_identity_cancels_the_old_picture_and_uses_only_the_canonical_one(
    monkeypatch, tmp_path
):
    activity_store.init(tmp_path / "activity.db")

    async def run():
        work = artist_work.ArtistWork()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def thumb(mbid, **kwargs):
            if mbid == MBID:
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()
            return fanart_tv.Thumb("https://assets.fanart.tv/canonical.jpg", "canonical")

        async def read():
            await wait(started)
            return {**LINKED, "id": MERGED}

        download = AsyncMock(return_value=JPEG)
        monkeypatch.setattr(fanart_tv, "artist_thumb", thumb)
        monkeypatch.setattr(artist_fill, "download_picture", download)
        try:
            answer, result = await artist_fill.fill(
                MBID,
                read,
                lacks_biography=False,
                lacks_picture=True,
                asking=ASKING,
                client=work.http,
                deadline=asyncio.get_running_loop().time() + 5,
            )
            assert answer["id"] == MERGED and cancelled.is_set()
            assert result.picture is not None and result.picture.credit.url == "canonical"
            assert download.await_count == 1
            assert download.call_args.args == ("https://assets.fanart.tv/canonical.jpg",)
        finally:
            await work.close()

    asyncio.run(run())


def test_cancellation_before_write_acceptance_leaves_the_file_untouched(tmp_path, monkeypatch):
    path = tmp_path / "artist.nfo"
    path.write_bytes(b"original")

    async def run():
        work = artist_work.ArtistWork()
        lock = asyncio.Lock()
        work._paths[path.resolve()] = lock
        await lock.acquire()
        try:
            task = asyncio.create_task(work.write(path, lambda: path.write_bytes(b"changed")))
            # Observe the actual path-lock wait, not a guessed delay.
            original_acquire = lock.acquire
            waiting = asyncio.Event()

            async def acquire():
                waiting.set()
                return await original_acquire()

            monkeypatch.setattr(lock, "acquire", acquire)
            await wait(waiting)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            lock.release()
            await work.close()
            assert path.read_bytes() == b"original"
        finally:
            if lock.locked():
                lock.release()
            await work.close()

    asyncio.run(run())


def test_lifespan_drains_cancelled_request_through_audit_history_and_inventory(
    artist_client, monkeypatch
):
    client, cfg, _network = artist_client
    app = client.app
    path = cfg.paths.music_dir / "Carol" / "artist.nfo"
    path.parent.mkdir()
    path.write_bytes(b"")
    key = id_registry.get_or_mint(path)
    monkeypatch.setattr(mb_lookup, "fetch_artist", Mock(return_value={"id": MBID, "name": "Carol"}))

    async def run():
        loop = asyncio.get_running_loop()
        at_history = asyncio.Event()
        release_history = threading.Event()
        refreshed = asyncio.Event()
        recorded = activity_store.record_nfo_changes
        refresh = app.state.scan_runner.refresh_nfo
        # Seed a real inventory before engaging the scanner, avoiding a race
        # between the fixture's first request and its initial background walk.
        await asyncio.to_thread(app.state.scan_runner.refresh_now)

        def record(*args, **kwargs):
            loop.call_soon_threadsafe(at_history.set)
            assert release_history.wait(5), "shutdown did not release the history insert"
            return recorded(*args, **kwargs)

        def publish(file):
            refresh(file)
            loop.call_soon_threadsafe(refreshed.set)

        monkeypatch.setattr(activity_store, "record_nfo_changes", record)
        monkeypatch.setattr(app.state.scan_runner, "refresh_nfo", publish)
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1",
                headers={"HX-Request": "true"},
            ) as browser:
                request = asyncio.create_task(
                    browser.post(f"/artist/{key}/identify", data={"file": key, "artist": MBID})
                )
                reached = asyncio.create_task(wait(at_history))
                done, _ = await asyncio.wait(
                    {request, reached}, return_when=asyncio.FIRST_COMPLETED
                )
                if request in done:
                    response = request.result()
                    reached.cancel()
                    pytest.fail(
                        f"request ended before the history insert: {response.status_code} {response.text}"
                    )
                await reached
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
                shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
                await checkpoint()
                assert not shutdown.done(), "shutdown abandoned an accepted write's history"
                release_history.set()
                await asyncio.wait_for(shutdown, 5)
                assert refreshed.is_set()
        finally:
            release_history.set()
            await app.state.artist_work.close()
        assert MBID.encode() in path.read_bytes()
        events = activity_store.artist_history(MBID)
        audit = [
            e for e in events if e.source is Source.AUDIT and e.message.startswith("nfo.write")
        ]
        outcomes = [
            e for e in events if e.source is Source.ACTIVITY and e.message.startswith("Identified")
        ]
        assert len(audit) == len(outcomes) == 1
        assert audit[0].action_id == outcomes[0].action_id is not None
        assert activity_store.nfo_changes_for([audit[0].id])[audit[0].id].changes
        inventory = app.state.scan_runner.artist_inventory(MBID)
        assert inventory is not None and inventory.by_key[MBID][0].path == path

    asyncio.run(run())
