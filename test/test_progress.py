"""Background work the status bar shows while it runs (#757).

A job registers a label and how far it has got; `/status` reports every running
job and the closing line of the last one to finish, and the bar renders them.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from harmonist import activity_store, gardener, mb_lookup, progress
from harmonist.config import BandcampConfig, Config, PathsConfig, ServerConfig
from harmonist.config import TestConfig as _TestConfig
from harmonist.transforms import TaggingChoices, TagTransform
from harmonist.web.main import create_app
from test.test_gardener import _release, _tagged


@pytest.fixture(autouse=True)
def _clean_register():
    progress.reset()
    yield
    progress.reset()


def _running() -> list[tuple[str, int, int]]:
    return [(j["label"], j["done"], j["total"]) for j in progress.status()["jobs"]]


# ---- the register ---------------------------------------------------------


def test_a_job_is_listed_with_its_progress_while_it_runs():
    with progress.job("Checking updates", total=3) as job:
        job.advance()
        job.advance()
        assert _running() == [("Checking updates", 2, 3)]
    assert _running() == []


def _in_thread(label: str, started: threading.Event, stop: threading.Event) -> threading.Thread:
    """A job on its own thread, as independent jobs run."""

    def run() -> None:
        with progress.job(label, total=1, foreground=True):
            started.set()
            stop.wait(5)

    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(5)
    return thread


def test_jobs_are_listed_in_the_order_they_started():
    stop = threading.Event()
    first = _in_thread("First", threading.Event(), stop)
    second = _in_thread("Second", threading.Event(), stop)
    try:
        assert [label for label, _, _ in _running()] == ["First", "Second"]
    finally:
        stop.set()
        first.join(5)
        second.join(5)


def test_a_job_started_inside_another_is_shown_as_its_step():
    """Applying updates writes each album's tags as a Tagging job of its own:
    the bar shows the album count with that album's tracks, not the tracks
    alone with the count hidden behind them."""
    with progress.job("Applying updates", total=3) as outer:
        outer.item = "Rawhide"
        with progress.job("Tagging", total=12, foreground=True) as inner:
            inner.advance(2)
            jobs = progress.status()["jobs"]
        after = progress.status()["jobs"]

    assert jobs == [
        {
            "label": "Applying updates",
            "done": 0,
            "total": 3,
            "item": "Rawhide",
            "foreground": True,
            "step": {"label": "Tagging", "done": 2, "total": 12},
        }
    ]
    assert after == [{"label": "Applying updates", "done": 0, "total": 3, "item": "Rawhide"}]


def test_a_job_on_another_thread_is_not_a_step():
    stop = threading.Event()
    with progress.job("Applying updates", total=3):
        other = _in_thread("Tagging", threading.Event(), stop)
        try:
            assert [label for label, _, _ in _running()] == ["Applying updates", "Tagging"]
        finally:
            stop.set()
            other.join(5)


def test_a_finished_job_leaves_its_closing_line_and_a_new_one_replaces_it():
    with progress.job("Checking updates", total=1) as job:
        job.closing = "4 albums have a tag update"
    first = progress.status()["finished"]
    with progress.job("Re-checking tags", total=1) as job:
        job.closing = "2 albums have a tag update"
    second = progress.status()["finished"]

    assert first is not None and second is not None
    assert (first["label"], first["message"]) == ("Checking updates", "4 albums have a tag update")
    assert (second["label"], second["message"]) == (
        "Re-checking tags",
        "2 albums have a tag update",
    )
    assert second["seq"] > first["seq"]


def test_a_job_that_fails_leaves_the_bar_and_no_closing_line():
    with pytest.raises(RuntimeError), progress.job("Checking updates", total=1) as job:
        job.closing = "never reached"
        raise RuntimeError("disk gone")

    assert _running() == []
    assert progress.status()["finished"] is None


# ---- the jobs that register ----------------------------------------------


def _stored_moved_release(tmp_path):
    activity_store.init(tmp_path / "activity.db")
    album = _tagged(tmp_path, _release())
    activity_store.store_release(
        "rel-aaa",
        "+".join(sorted(mb_lookup.RELEASE_INCLUDES)),
        _release("Test Album (remastered)"),
    )
    conn = activity_store._ensure()
    conn.execute(
        "UPDATE mb_release_cache SET fetched_at = ?",
        ((datetime.now(UTC) - timedelta(days=7)).isoformat(),),
    )
    conn.commit()
    return album


def test_the_startup_update_check_reports_progress_and_what_it_found(tmp_path, monkeypatch):
    album = _stored_moved_release(tmp_path)
    seen: list[list[tuple[str, int, int]]] = []
    real = gardener.refresh_flag

    def _watching(*args, **kwargs):
        seen.append(_running())
        return real(*args, **kwargs)

    monkeypatch.setattr(gardener, "refresh_flag", _watching)
    gardener.warm_from_cache([album], ignored={})

    assert seen == [[("Checking for tag updates", 0, 1)]]
    finished = progress.status()["finished"]
    assert finished is not None
    assert finished["message"] == "1 album has a tag update"


def test_the_startup_check_counts_what_the_library_and_inbox_show(tmp_path):
    """#818 through the app's own startup hook, so it fails if the ignored
    updates or the held ones are not handed to the check, not only if the check
    miscounts them. Two albums with an update: one the user ignored, which the
    Library's filter leaves out, and one auto-apply held back, which the Inbox
    shows."""
    from harmonist import auto_apply
    from harmonist.formats import owned
    from harmonist.web import main

    cfg = Config(
        paths=PathsConfig(config_dir=tmp_path / "config", music_dir=tmp_path / "music"),
        bandcamp=BandcampConfig(),
        server=ServerConfig(),
        test=_TestConfig(mode="fixture"),
    )
    cfg.gardener.apply_up_to = owned.Significance.IDENTITY
    cfg.paths.music_dir.mkdir(parents=True)
    cfg.paths.config_dir.mkdir(parents=True)
    client = TestClient(create_app(cfg), headers={"HX-Request": "true"})
    for name in ("Ignored", "Held"):
        release = _release(mbid=f"rel-{name}")
        _tagged(cfg.paths.music_dir, release, name=name)
        activity_store.store_release(
            release["id"],
            "+".join(sorted(mb_lookup.RELEASE_INCLUDES)),
            {**release, "title": "Test Album (remastered)"},
        )
    runner = client.app.state.scan_runner
    runner.refresh_now()
    albums = {a.path.name: a for a in runner.albums()}
    checked = gardener.warm_from_cache(list(albums.values()), ignored={})
    albums = {a.path.name: a for a in checked.albums}
    activity_store.ignore_update(
        albums["Ignored"].id, release_version=albums["Ignored"].mb_version or ""
    )
    auto_apply._hold(albums["Held"], "Track assignments need review")
    progress.reset()

    try:
        main._start_flag_warm_up(runner)
        for _ in range(100):
            if progress.status()["finished"] is not None:
                break
            time.sleep(0.05)
    finally:
        auto_apply.reset()

    finished = progress.status()["finished"]
    assert finished is not None
    assert finished["message"] == "1 album has a tag update, 1 for review"


def test_the_settings_recheck_reports_what_it_found(tmp_path):
    album = _stored_moved_release(tmp_path)
    before = TaggingChoices()
    after = TaggingChoices(transforms=frozenset({TagTransform.ALBUM_DISAMBIGUATION}))

    gardener.recheck_for_settings([album], before, after)

    finished = progress.status()["finished"]
    assert finished is not None
    # The album's release has no disambiguation, so the transform cannot move it.
    assert (finished["label"], finished["message"]) == ("Re-checking tags", "no albums affected")


def test_the_settings_recheck_counts_only_the_albums_the_setting_moves(tmp_path):
    activity_store.init(tmp_path / "activity.db")
    disambiguated = {**_release(), "disambiguation": "expanded edition"}
    album = _tagged(tmp_path, disambiguated)
    activity_store.store_release(
        "rel-aaa", "+".join(sorted(mb_lookup.RELEASE_INCLUDES)), disambiguated
    )
    before = TaggingChoices()
    after = TaggingChoices(transforms=frozenset({TagTransform.ALBUM_DISAMBIGUATION}))
    # As the app has it by the time the re-check runs: the new setting is live.
    gardener.configure(lambda: after)

    gardener.recheck_for_settings([album], before, after)

    finished = progress.status()["finished"]
    assert finished is not None
    assert finished["message"] == "1 album affected, 1 with a tag update"


def test_the_artwork_warm_up_shows_only_while_it_has_releases_to_read(monkeypatch):
    from types import SimpleNamespace

    from harmonist import artwork_updates

    seen: list[list[tuple[str, int, int]]] = []
    read: set[str] = set()

    def _observe(mbid):
        seen.append(_running())
        read.add(mbid)

    monkeypatch.setattr(artwork_updates, "observe", _observe)
    monkeypatch.setattr(artwork_updates, "_observed", {"rel-known": ()})
    albums = [
        SimpleNamespace(sidecar=SimpleNamespace(mb_release_id=mbid))
        for mbid in ("rel-a", "rel-known", "rel-b")
    ]

    artwork_updates.warm(albums)  # type: ignore[arg-type]

    assert seen == [[("Reading artwork", 0, 2)], [("Reading artwork", 1, 2)]]
    assert read == {"rel-a", "rel-b"}


def test_an_artwork_warm_up_with_nothing_to_read_is_not_shown(monkeypatch):
    from types import SimpleNamespace

    from harmonist import artwork_updates

    started: list[str] = []
    real_job = progress.job

    def _job(label, total):
        started.append(label)
        return real_job(label, total)

    monkeypatch.setattr(progress, "job", _job)
    monkeypatch.setattr(artwork_updates, "_observed", {"rel-known": ()})

    artwork_updates.warm([SimpleNamespace(sidecar=SimpleNamespace(mb_release_id="rel-known"))])  # type: ignore[list-item]

    assert started == []


# ---- /status ----------------------------------------------------------------


def test_status_reports_running_jobs(tmp_path):
    cfg = Config(
        paths=PathsConfig(config_dir=tmp_path / "config", music_dir=tmp_path / "music"),
        bandcamp=BandcampConfig(),
        server=ServerConfig(),
        test=_TestConfig(mode="fixture"),
    )
    cfg.paths.music_dir.mkdir(parents=True)
    cfg.paths.config_dir.mkdir(parents=True)
    client = TestClient(create_app(cfg), headers={"HX-Request": "true"})

    with progress.job("Checking for tag updates", total=10) as job:
        job.advance(3)
        body = client.get("/status").json()

    assert body["progress"]["jobs"] == [
        {"label": "Checking for tag updates", "done": 3, "total": 10}
    ]


def test_every_page_polls_the_status_bar(tmp_path):
    """A job started from Settings, or an action taken on an album page, shows in
    the bar where it was started, not only once the user is back on the main
    page."""
    from test.test_web import _make_tagged_album

    cfg = Config(
        paths=PathsConfig(config_dir=tmp_path / "config", music_dir=tmp_path / "music"),
        bandcamp=BandcampConfig(),
        server=ServerConfig(),
        test=_TestConfig(mode="fixture"),
    )
    cfg.paths.music_dir.mkdir(parents=True)
    cfg.paths.config_dir.mkdir(parents=True)
    _make_tagged_album(cfg, "Paged", mbid="rel-paged", tagged_at=datetime.now(UTC))
    client = TestClient(create_app(cfg), headers={"HX-Request": "true"})

    for page in ("/", "/settings", "/about", "/album/rel-paged"):
        html = client.get(page).text
        assert html.count('id="app-status"') == 1, page
        assert html.count("window.harmonistStatusBar = true") == 1, page


@pytest.mark.parametrize("artwork", [False, True])
def test_file_writes_report_each_track_and_clear_progress(tmp_path, monkeypatch, artwork):
    from harmonist import formats, tagger
    from test.test_artwork import png_bytes
    from test.test_gardener import _album_dir
    from test.test_tagger import _update_artwork

    album = _album_dir(tmp_path, tracks=2)
    release = _release(tracks=2)
    activity_store.init(tmp_path / "activity.db")
    seen = []
    write_name = "write_cover" if artwork else "write_tags"
    original = getattr(formats, write_name)

    def writing(path, *args, **kwargs):
        seen.append(progress.status()["jobs"])
        return original(path, *args, **kwargs)

    monkeypatch.setattr(formats, write_name, writing)
    if artwork:
        cover = album / "cover.png"
        cover.write_bytes(png_bytes(10, 10))
        _update_artwork(album, release, cover)
    else:
        tagger.tag_album(album, release)

    label = "Applying artwork" if artwork else "Tagging"
    assert seen == [
        [
            {
                "label": label,
                "done": index,
                "total": 2,
                "item": f"0{index + 1} Track {index + 1}.m4a",
                "foreground": True,
            }
        ]
        for index in range(2)
    ]
    assert progress.status()["jobs"] == []
