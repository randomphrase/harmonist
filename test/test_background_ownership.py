"""Owned workers finish accepted albums, not the rest of a stopped pass."""

import asyncio
import threading
from functools import partial

import pytest

from harmonist import activity_store, artwork_updates, auto_apply, background, gardener
from harmonist.config import GardenerConfig
from harmonist.formats import owned
from harmonist.web import main as web_main
from test.test_auto_apply import _catalogue_number, _catalogued
from test.test_gardener import _release, _store, _tagged
from test.test_web import cfg  # noqa: F401 (fixture)


def test_a_failed_watcher_does_not_prevent_shutdown_draining_jobs(cfg, monkeypatch, caplog):  # noqa: F811
    cfg.paths.music_dir.mkdir(parents=True)
    cfg.paths.config_dir.mkdir(parents=True)
    app = web_main.create_app(cfg)
    monkeypatch.setattr(web_main.log, "handlers", [*web_main.log.handlers, caplog.handler])
    runner = app.state.scan_runner
    runner.set_on_first_complete(lambda: None)

    async def failed_watch(*args, **kwargs):
        raise RuntimeError("watcher failed before shutdown")

    monkeypatch.setattr(web_main.dir_watcher, "watch_music_dir", failed_watch)

    async def run():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        completed = []
        original_wait = runner.jobs.wait_idle

        async def drain():
            release.set()
            await original_wait()

        def accepted():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
            completed.append("finished")

        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        await asyncio.wait_for(runner._task, 5)
        monkeypatch.setattr(runner.jobs, "wait_idle", drain)
        assert runner.jobs.start("accepted-write", accepted)
        await asyncio.wait_for(entered.wait(), 5)
        try:
            result = await asyncio.gather(
                lifespan.__aexit__(None, None, None), return_exceptions=True
            )
            assert result[0] is False
            assert completed == ["finished"]
        finally:
            release.set()
            await runner.jobs.close()

    asyncio.run(run())
    assert "watcher failed before shutdown" in caplog.text


def test_shutdown_drains_an_accepted_album_and_refuses_the_next(monkeypatch):
    async def run():
        jobs = background.Jobs(workers=1)
        loop = asyncio.get_running_loop()
        entered, draining = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        completed = []
        original = jobs.wait_idle

        async def wait():
            draining.set()
            await original()

        monkeypatch.setattr(jobs, "wait_idle", wait)

        def pass_():
            for album in background.items(["accepted", "queued"]):
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(5)
                completed.append(album)

        assert jobs.start("apply", pass_)
        await asyncio.wait_for(entered.wait(), 5)
        closing = asyncio.create_task(jobs.close())
        try:
            await asyncio.wait_for(draining.wait(), 5)
            assert not closing.done()
            assert not jobs.start("another", lambda: completed.append("unwanted"))
        finally:
            release.set()
            await asyncio.wait_for(closing, 5)
        assert completed == ["accepted"]

    asyncio.run(run())


def test_repeated_requests_coalesce_without_allocating_more_workers():
    async def run():
        jobs = background.Jobs(workers=1)
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        completed: list[str | int] = []

        def first():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
            completed.append("first")

        assert jobs.start("recheck", first)
        await asyncio.wait_for(entered.wait(), 5)
        try:
            for i in range(20):
                assert jobs.start("recheck", partial(completed.append, i), repeat=True)
            assert not jobs.start("recheck", lambda: completed.append("duplicate"))
        finally:
            release.set()
            await asyncio.wait_for(jobs.wait_idle(), 5)
            await jobs.close()
        assert completed == ["first", 19]

    asyncio.run(run())


def test_independent_jobs_keep_separate_per_album_action_scopes():
    async def run():
        jobs = background.Jobs(workers=1)
        actions = []

        def pass_():
            for _ in background.items(range(2)):
                with activity_store.action() as action:
                    actions.append(action)

        with activity_store.action() as request:
            assert jobs.start("apply", pass_)
        await asyncio.wait_for(jobs.wait_idle(), 5)
        await jobs.close()
        assert len(set(actions)) == 2
        assert request not in actions

    asyncio.run(run())


@pytest.mark.parametrize("cancel_waiter", [False, True])
def test_lifespan_drains_tag_history_audit_and_inventory(cfg, monkeypatch, cancel_waiter):  # noqa: F811
    cfg.paths.music_dir.mkdir(parents=True)
    cfg.paths.config_dir.mkdir(parents=True)
    app = web_main.create_app(cfg)
    _tagged(cfg.paths.music_dir, _release())
    _store(_catalogued())
    runner = app.state.scan_runner

    async def run():
        loop = asyncio.get_running_loop()
        at_history, stopping, draining = asyncio.Event(), asyncio.Event(), asyncio.Event()
        finished = asyncio.Event()
        release = threading.Event()
        record = activity_store.record_tag_changes
        stop = runner.jobs.stop
        wait_idle = runner.jobs.wait_idle
        close_scan = runner.close

        async def close_scanner():
            await close_scan()
            finished.set()

        async def waiting():
            draining.set()
            await wait_idle()

        def held(*args, **kwargs):
            loop.call_soon_threadsafe(at_history.set)
            assert release.wait(5)
            return record(*args, **kwargs)

        def stop_jobs():
            stop()
            loop.call_soon_threadsafe(stopping.set)

        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        closing = None
        try:
            await asyncio.wait_for(runner._task, 5)
            await asyncio.wait_for(runner.jobs.wait_idle(), 5)
            album = runner.albums()[0]
            await asyncio.to_thread(gardener.refresh_flag, album, _catalogued())
            app.state.cfg = cfg.model_copy(
                update={"gardener": GardenerConfig(apply_up_to=owned.Significance.ENRICHMENT)}
            )
            monkeypatch.setattr(activity_store, "record_tag_changes", held)
            monkeypatch.setattr(runner.jobs, "stop", stop_jobs)
            assert (
                web_main._auto_apply_if_idle(
                    app, app.state.sync_runner, app.state.reconcile_runner, runner
                )
                is None
            )
            await asyncio.wait_for(at_history.wait(), 5)
            assert _catalogue_number(album) == "WARPCD-999"
            monkeypatch.setattr(runner.jobs, "wait_idle", waiting)
            monkeypatch.setattr(runner, "close", close_scanner)
            closing = asyncio.create_task(lifespan.__aexit__(None, None, None))
            await asyncio.wait_for(stopping.wait(), 5)
            await asyncio.wait_for(draining.wait(), 5)
            assert not closing.done(), "shutdown abandoned the accepted history write"
            assert not runner.jobs.start("late-write", lambda: None)
            if cancel_waiter:
                closing.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await closing
        finally:
            release.set()
            if closing is None:
                closing = asyncio.create_task(lifespan.__aexit__(None, None, None))
            if not closing.cancelled():
                await asyncio.wait_for(closing, 5)
            await asyncio.wait_for(finished.wait(), 5)
            monkeypatch.setattr(runner.jobs, "stop", stop)

        completed = [
            e
            for e in activity_store.recent(50)
            if e.message == "Tag update applied automatically (enrichment)"
        ]
        assert len(completed) == 1
        outcome = completed[0]
        assert outcome.action_id is not None
        audit = activity_store.audit_by_action([outcome.action_id])[outcome.action_id]
        assert any(e.message.startswith("tag.album") for e in audit)
        assert activity_store.tag_changes_for([e.id for e in audit])
        assert not runner.albums()[0].update_available
        assert runner.albums()[0].revision is not album.revision

    asyncio.run(run())


@pytest.mark.parametrize(
    "job",
    [
        "tag-check",
        "artwork-check",
        "warm-up",
        "rebuilt",
        "settings",
        "auto-apply",
        "bulk-tags",
        "bulk-artwork",
        "sync",
        "reconcile",
    ],
)
def test_every_background_entry_point_belongs_to_lifespan(cfg, monkeypatch, job):  # noqa: F811
    cfg.paths.music_dir.mkdir(parents=True)
    cfg.paths.config_dir.mkdir(parents=True)
    cfg = cfg.model_copy(
        update={
            "gardener": GardenerConfig(level="review", apply_up_to=owned.Significance.ENRICHMENT)
        }
    )
    app = web_main.create_app(cfg)
    _tagged(cfg.paths.music_dir, _release())
    runner = app.state.scan_runner
    runner.set_on_first_complete(lambda: None)

    async def run():
        loop = asyncio.get_running_loop()
        entered, stopping, draining = asyncio.Event(), asyncio.Event(), asyncio.Event()
        release = threading.Event()
        completed = []
        original_stop = runner.jobs.stop
        original_wait = runner.jobs.wait_idle

        async def wait():
            draining.set()
            await original_wait()

        def held(*args, **kwargs):
            try:
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(5)
                completed.append(job)
            finally:
                if job == "bulk-tags":
                    auto_apply.release_claim()
                if job == "bulk-artwork":
                    auto_apply.release_artwork_claim()

        def stop():
            original_stop()
            loop.call_soon_threadsafe(stopping.set)

        target = {
            "tag-check": (gardener, "sweep"),
            "artwork-check": (artwork_updates, "sweep"),
            "warm-up": (gardener, "warm_from_cache"),
            "rebuilt": (gardener, "recheck_rebuilt"),
            "settings": (gardener, "recheck_for_settings"),
            "auto-apply": (auto_apply, "apply_due"),
            "bulk-tags": (auto_apply, "write_claimed"),
            "bulk-artwork": (auto_apply, "write_claimed_artwork"),
            "sync": (app.state.sync_runner, "_runner_fn"),
            "reconcile": (app.state.reconcile_runner, "_runner_fn"),
        }[job]
        monkeypatch.setattr(*target, held)
        monkeypatch.setattr(runner.jobs, "stop", stop)
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        closing = None
        try:
            await asyncio.wait_for(runner._task, 5)
            albums = runner.albums()
            arguments = (app, app.state.sync_runner, app.state.reconcile_runner, runner)
            starts = {
                "tag-check": lambda: web_main._update_check_if_idle(*arguments),
                "artwork-check": lambda: web_main._artwork_check_if_idle(*arguments),
                "warm-up": lambda: web_main._start_flag_warm_up(runner),
                "rebuilt": lambda: web_main._start_rebuilt_recheck(runner, albums),
                "settings": lambda: web_main._start_settings_recheck(
                    runner, cfg.tagging.choices(), cfg.tagging.choices()
                ),
                "auto-apply": lambda: web_main._auto_apply_if_idle(*arguments),
                "bulk-tags": lambda: web_main._start_bulk_apply(app, {albums[0].id: None}),
                "bulk-artwork": lambda: web_main._start_bulk_artwork_apply(
                    app, {albums[0].id: "selected"}
                ),
                "sync": app.state.sync_runner.start,
                "reconcile": app.state.reconcile_runner.start,
            }
            starts[job]()
            await asyncio.wait_for(entered.wait(), 5)
            monkeypatch.setattr(runner.jobs, "wait_idle", wait)
            closing = asyncio.create_task(lifespan.__aexit__(None, None, None))
            await asyncio.wait_for(stopping.wait(), 5)
            await asyncio.wait_for(draining.wait(), 5)
            assert not closing.done(), f"shutdown lost ownership of {job}"
        finally:
            release.set()
            if closing is None:
                closing = asyncio.create_task(lifespan.__aexit__(None, None, None))
            await asyncio.wait_for(closing, 5)
            monkeypatch.setattr(runner.jobs, "stop", original_stop)
        assert completed == [job]

    asyncio.run(run())
