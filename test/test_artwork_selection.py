"""Applying a selection of artwork updates from the Inbox (#859).

The artwork counterpart of #814: tick artwork cards in the Inbox and apply them
together. A card carries the plan it showed, and an album whose plan has moved
since is left alone. The selection is a review, so replacements and watched
albums can be applied, under the artwork store's backup rule, as on the album
page. Failures are reported, not held.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

import pytest

from harmonist import (
    activity,
    activity_store,
    album_artwork,
    artwork_store,
    artwork_updates,
    auto_apply,
    caa_cache,
    cover_art,
    formats,
    images,
    progress,
    scanner,
)
from harmonist import sidecar as sidecar_mod
from harmonist.models import ArtworkWatch
from test import test_web
from test.test_artwork import png_bytes
from test.test_artwork_auto_apply import (
    ARCHIVE,
    MBID,
    _archive,
    _art,
    _config,
    _library,
    _no_requests,
    _routing,
    _track,
)

cfg = test_web.cfg
client = test_web.client


@pytest.fixture(autouse=True)
def _clean(tmp_path):
    activity_store.init(tmp_path / "activity.db")
    cover_art.configure_cache(tmp_path / "caa")
    caa_cache.configure(timedelta(days=7))
    artwork_store.configure(tmp_path / "backups")
    auto_apply.reset()
    progress.reset()
    yield
    auto_apply.reset()
    progress.reset()
    cover_art.configure_cache(None)
    artwork_store.configure(None)


ROUTING = _routing(review="addition", apply="none")


def _apply(root, albums, chosen=None) -> auto_apply.ChosenResult:
    """Claim and write a selection, each album against its card's plan unless
    `chosen` says otherwise."""
    if chosen is None:
        chosen = {a.id: artwork_updates.fingerprint(a) for a in albums}
    todo = auto_apply.claim_artwork(albums, chosen)
    assert todo is not None
    return auto_apply.write_claimed_artwork(todo, chosen, _config(root, ROUTING))


def _entries(album) -> list[str]:
    return [e.message for e in activity.recent(20) if e.album_id == album.id]


# ---- the pass -------------------------------------------------------------


def test_a_selected_addition_is_written_as_its_own_undoable_action(tmp_path, monkeypatch):
    _archive()
    bare = _library(tmp_path, ROUTING, Bare=None)["Bare"]
    _no_requests(monkeypatch)

    assert _apply(tmp_path, [bare]) == auto_apply.ChosenResult(applied=1, failed=0)

    assert _art(bare) == images.digest(ARCHIVE)
    entry = next(e for e in activity.recent(20) if e.album_id == bare.id)
    assert entry.message == "Updated artwork on 1 file"
    assert entry.action_id is not None
    audit = activity_store.audit_by_action([entry.action_id])[entry.action_id]
    assert any(r.message.startswith("artwork.update") for r in audit)


def test_applying_the_same_selection_again_writes_and_says_nothing(tmp_path):
    _archive()
    bare = _library(tmp_path, ROUTING, Bare=None)["Bare"]
    chosen = {bare.id: artwork_updates.fingerprint(bare)}
    assert _apply(tmp_path, [bare], chosen).applied == 1
    count = len(activity.recent(50))
    track = _track(bare).stat().st_mtime_ns

    again = _apply(tmp_path, [bare], chosen)

    assert again == auto_apply.ChosenResult(applied=0, failed=0)
    assert len(activity.recent(50)) == count
    assert _track(bare).stat().st_mtime_ns == track


def test_a_selected_replacement_is_written_with_its_backup_kept(tmp_path):
    _archive()
    small = png_bytes(100, 100)
    album = _library(tmp_path, ROUTING, Small=small)["Small"]
    assert artwork_updates.operation(album) is artwork_updates.ORDER[-1]

    assert _apply(tmp_path, [album]).applied == 1

    assert (album.path / "cover.png").read_bytes() == ARCHIVE
    assert artwork_store.path_for(images.digest(small)) is not None
    assert _art(album) is not None  # its addition, from the plan, as the page writes it


def test_a_selected_watched_album_is_written_and_its_watch_ends(tmp_path):
    _archive()
    bare = _library(tmp_path, ROUTING, Bare=None)["Bare"]
    sc = sidecar_mod.read(bare.path)
    assert sc is not None
    sidecar_mod.write(bare.path, replace(sc, artwork_watch=ArtworkWatch(MBID)))
    watched = next(a for a in scanner.scan(tmp_path) if a.path == bare.path)

    assert _apply(tmp_path, [watched]).applied == 1

    assert _art(watched) == images.digest(ARCHIVE)
    after = sidecar_mod.read(bare.path)
    assert after is not None and after.artwork_watch is None


def test_a_plan_that_moved_since_it_was_selected_is_left_alone(tmp_path):
    _archive()
    bare = _library(tmp_path, ROUTING, Bare=None)["Bare"]

    result = _apply(tmp_path, [bare], {bare.id: "a-plan-the-card-no-longer-shows"})

    assert result == auto_apply.ChosenResult(applied=0, failed=1)
    assert _art(bare) is None
    assert _entries(bare) == ["Artwork not applied: Its artwork changed after it was selected"]


def test_files_changed_since_the_card_was_drawn_are_left_alone(tmp_path):
    _archive()
    bare = _library(tmp_path, ROUTING, Bare=None)["Bare"]
    chosen = {bare.id: artwork_updates.fingerprint(bare)}
    # Larger than the archive's: a smaller one would now be a replacement on
    # this lossless album (#872), which the card's fingerprint refuses.
    mine = png_bytes(700, 700)
    formats.write_cover(_track(bare), mine)  # after the card was drawn

    assert _apply(tmp_path, [bare], chosen).failed == 0  # nothing left to add

    assert _art(bare) == images.digest(mine)


def test_a_folder_cover_added_since_the_card_was_drawn_refuses_the_write(tmp_path):
    _archive()
    bare = _library(tmp_path, ROUTING, Bare=None)["Bare"]
    small = png_bytes(100, 100)
    (bare.path / "cover.png").write_bytes(small)

    assert _apply(tmp_path, [bare]).failed == 1

    assert (bare.path / "cover.png").read_bytes() == small
    assert _art(bare) is None


def test_a_failed_write_is_reported_and_not_held(tmp_path, monkeypatch):
    _archive()
    bare = _library(tmp_path, ROUTING, Bare=None)["Bare"]
    monkeypatch.setattr(album_artwork, "apply", Mock(side_effect=OSError("disk full")))

    assert _apply(tmp_path, [bare]).failed == 1

    assert _entries(bare) == ["Artwork not applied: Couldn't be written: disk full"]
    assert not auto_apply.is_artwork_held(bare)


def test_an_album_not_in_the_selection_is_not_written(tmp_path):
    _archive()
    albums = _library(tmp_path, ROUTING, One=None, Two=None)
    one, two = albums["One"], albums["Two"]

    _apply(tmp_path, [one, two], {one.id: artwork_updates.fingerprint(one)})

    assert _art(one) == images.digest(ARCHIVE)
    assert _art(two) is None


def test_each_card_knows_where_it_stands_apart_from_the_tag_cards(tmp_path, monkeypatch):
    _archive()
    albums = _library(tmp_path, ROUTING, One=None, Two=None)
    one, two = albums["One"], albums["Two"]
    seen = []
    real = album_artwork.apply

    def watch(album, *args, **kwargs):
        seen.append(
            (
                auto_apply.artwork_queued(one),
                auto_apply.artwork_queued(two),
                auto_apply.queued(one),
                auto_apply.is_applying(),
            )
        )
        return real(album, *args, **kwargs)

    monkeypatch.setattr(album_artwork, "apply", watch)

    _apply(tmp_path, [one, two])

    assert seen[0][:2] in {("applying", "queued"), ("queued", "applying")}
    assert seen[0][2] is None  # the album's tag card is not part of this
    assert seen[0][3]
    assert auto_apply.artwork_queued(one) is None and not auto_apply.is_applying()


def test_a_selection_waits_for_no_pass_already_writing(tmp_path):
    _archive()
    bare = _library(tmp_path, ROUTING, Bare=None)["Bare"]
    auto_apply._pass_lock.acquire()
    try:
        assert auto_apply.claim_artwork([bare], {bare.id: "x"}) is None
    finally:
        auto_apply._pass_lock.release()
    assert auto_apply.artwork_queued(bare) is None


# ---- the route and the Inbox ----------------------------------------------


def _status(response) -> dict:
    return json.loads(response.headers["HX-Trigger"])["harmonist-status"]


def test_applying_a_selection_starts_the_pass_with_each_cards_plan(client, monkeypatch):
    from harmonist.web import main as web_main

    started = []
    monkeypatch.setattr(
        web_main, "_start_bulk_artwork_apply", lambda app, chosen: started.append(chosen)
    )
    scans = []
    monkeypatch.setattr(client.app.state.scan_runner, "request_scan", lambda: scans.append(1))

    response = client.post("/artwork-updates/apply", data={"album": ["rel-a:f1", "rel-b:f2"]})

    assert started == [{"rel-a": "f1", "rel-b": "f2"}]
    assert (_status(response)["verb"], _status(response)["details"]) == (
        "Applying artwork",
        "2 albums selected",
    )
    assert scans == []


def test_nothing_selected_applies_nothing(client, monkeypatch):
    from harmonist.web import main as web_main

    started = []
    monkeypatch.setattr(
        web_main, "_start_bulk_artwork_apply", lambda app, chosen: started.append(chosen)
    )

    response = client.post("/artwork-updates/apply", data={})

    assert started == []
    assert _status(response)["verb"] == "Nothing applied"


def test_a_selection_is_not_applied_during_a_sync(client, monkeypatch):
    monkeypatch.setattr(type(client.app.state.sync_runner), "is_running", property(lambda s: True))
    claimed = []
    monkeypatch.setattr(auto_apply, "claim_artwork", lambda *a: claimed.append(a))

    response = client.post("/artwork-updates/apply", data={"album": ["rel-a:f1"]})

    assert claimed == []
    assert (_status(response)["verb"], _status(response)["level"]) == ("Not applied", "warning")


def test_a_card_is_selected_with_the_plan_it_shows(client, cfg):
    _archive()
    client.app.state.cfg = client.app.state.cfg.model_copy(update={"gardener": ROUTING})
    small = png_bytes(100, 100)
    albums = _library(cfg.paths.music_dir, ROUTING, Bare=None, Small=small)

    html = client.get("/tasks").text

    for name, op in (("Bare", "addition"), ("Small", "replacement")):
        album = albums[name]
        form = re.search(
            rf'<form id="artwork-group-{op}"[^>]*hx-post="/artwork-updates/apply".*?</form>',
            html,
            re.DOTALL,
        )
        assert form is not None
        box = re.search(
            rf'<input[^>]*data-pick="artwork:{re.escape(album.id)}"[^>]*>', form.group(0)
        )
        assert box is not None
        fingerprint = artwork_updates.fingerprint(album)
        assert f'value="{album.id}:{fingerprint}"' in box.group(0)
        assert "disabled" not in re.sub(r'"[^"]*"', '""', box.group(0))


def test_while_a_selection_is_applied_its_cards_say_where_they_stand(client, cfg):
    _archive()
    client.app.state.cfg = client.app.state.cfg.model_copy(update={"gardener": ROUTING})
    albums = _library(cfg.paths.music_dir, ROUTING, One=None, Two=None)
    one, two = albums["One"], albums["Two"]
    assert auto_apply.claim_artwork([one, two], {one.id: "x", two.id: "y"}) is not None
    with auto_apply._queue_lock:
        auto_apply._artwork_queue[one.id] = "applying"
    try:
        html = client.get("/tasks").text
    finally:
        with auto_apply._queue_lock:
            auto_apply._artwork_queue.clear()
        auto_apply._pass_lock.release()

    card = re.search(rf'<div id="task-artwork-{re.escape(one.id)}"[^>]*>', html)
    assert card is not None and "art-card--applying" in card.group(0)
    waiting = re.search(rf'<div id="task-artwork-{re.escape(two.id)}"[^>]*>', html)
    assert waiting is not None and "art-card--queued" in waiting.group(0)
    assert "Applying…" in html
    boxes = re.findall(r'<input[^>]*class="update-pick"[^>]*>', html)
    assert boxes and all("disabled" in re.sub(r'"[^"]*"', '""', b) for b in boxes)


def test_the_bulk_artwork_pass_runs_off_the_request_thread(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from harmonist.background import Jobs
    from harmonist.web.main import _start_bulk_artwork_apply

    written = threading.Event()
    monkeypatch.setattr(auto_apply, "write_claimed_artwork", lambda *a, **k: written.set())
    idle = Mock(is_running=False)
    runner = Mock(has_completed=lambda: True, albums=list, jobs=Jobs())
    app = Mock(
        state=SimpleNamespace(
            cfg=_config(tmp_path, ROUTING),
            sync_runner=idle,
            reconcile_runner=idle,
            scan_runner=runner,
        )
    )
    try:
        assert _start_bulk_artwork_apply(app, {"x": "y"}) is None
        assert written.wait(5)
    finally:
        if auto_apply._pass_lock.locked():
            auto_apply._pass_lock.release()
