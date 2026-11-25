"""Applying the Inbox tag updates the user selected, several at once (#814).

The selection is a review: each card showed the changes its update would make,
judged against the stored release at one MusicBrainz version. So the pass
writes from that stored release, tags only, and leaves alone any update that
has moved since it was selected. Structure is never offered.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import threading

import pytest

from harmonist import activity, activity_store, auto_apply, formats, gardener, mb_lookup, progress
from harmonist import retag as retag_mod
from harmonist.config import GardenerConfig
from harmonist.formats import owned
from harmonist.models import AlbumState
from harmonist.tagger import PicardCompatibleTagger
from test.test_auto_apply import _catalogue_number, _catalogued, _cfg, _flagged, _no_musicbrainz
from test.test_gardener import _release
from test.test_inbox_update_summary import _inbox_library, _row, web  # noqa: F401  (fixture)

S = owned.Significance


@pytest.fixture(autouse=True)
def _clean():
    auto_apply.reset()
    progress.reset()
    yield
    auto_apply.reset()
    progress.reset()


def _apply(tmp_path, albums, chosen=None, *, on_processed=None):
    """As the route does it, without the thread: claim, then write."""
    chosen = {a.id: a.mb_version for a in albums} if chosen is None else chosen
    todo = auto_apply.claim(albums, chosen)
    if todo is None:
        return None
    return auto_apply.write_claimed(
        todo, chosen, _cfg(tmp_path), PicardCompatibleTagger(), {}, on_processed=on_processed
    )


def _entries(album):
    return [e for e in activity.recent(20) if e.album_label == album.label]


def test_a_selected_update_is_written_from_the_stored_release(tmp_path, monkeypatch):
    """Whatever the apply threshold says: the user chose this one."""
    album = _flagged(tmp_path, _catalogued())
    _no_musicbrainz(monkeypatch)

    result = _apply(tmp_path, [album])

    assert result is not None and (result.applied, result.failed) == (1, 0)
    assert _catalogue_number(album) == "WARPCD-999"
    assert gardener.refresh_flag(album, _catalogued()).album.update_available is False


def test_each_applied_update_is_its_own_undoable_action(tmp_path, monkeypatch):
    album = _flagged(tmp_path, _catalogued())
    _no_musicbrainz(monkeypatch)

    _apply(tmp_path, [album])

    entries = _entries(album)
    assert [e.message for e in entries] == ["Tag update applied (enrichment)"]
    assert entries[0].action_id is not None
    audit = activity_store.audit_by_action([entries[0].action_id])[entries[0].action_id]
    assert any(e.message.startswith("tag.album") for e in audit)


def test_applying_the_same_selection_again_is_a_no_op(tmp_path, monkeypatch):
    album = _flagged(tmp_path, _catalogued())
    _no_musicbrainz(monkeypatch)
    chosen = {album.id: album.mb_version}

    _apply(tmp_path, [album], chosen)
    entries = len(activity.recent(50, include_audit=True))
    again = _apply(tmp_path, [album], chosen)

    assert again is not None and (again.applied, again.failed) == (0, 0)
    assert len(activity.recent(50, include_audit=True)) == entries


def test_an_update_that_moved_since_it_was_selected_is_left_alone(tmp_path, monkeypatch):
    album = _flagged(tmp_path, _catalogued())
    _no_musicbrainz(monkeypatch)

    result = _apply(tmp_path, [album], {album.id: "the version the card showed"})

    assert result is not None and (result.applied, result.failed) == (0, 1)
    assert _catalogue_number(album) is None
    assert [(e.level, e.message) for e in _entries(album)] == [
        (
            activity.Level.WARNING,
            "Tag update not applied: MusicBrainz changed after it was selected",
        )
    ]


def test_a_structure_update_is_never_applied_from_a_selection(tmp_path, monkeypatch):
    """Not offered, so only a hand-built request can ask."""
    album = _flagged(tmp_path, _catalogued())
    album.update_significance = S.STRUCTURE
    _no_musicbrainz(monkeypatch)

    result = _apply(tmp_path, [album])

    assert result is not None and (result.applied, result.failed) == (0, 1)
    assert _catalogue_number(album) is None


def test_an_album_not_in_the_selection_is_not_written(tmp_path, monkeypatch):
    album = _flagged(tmp_path, _catalogued())
    _no_musicbrainz(monkeypatch)

    result = _apply(tmp_path, [album], {})

    assert result is not None and (result.applied, result.failed) == (0, 0)
    assert _catalogue_number(album) is None


def test_an_incomplete_album_is_written_as_its_page_writes_it(tmp_path, monkeypatch):
    album = _flagged(tmp_path, _catalogued())
    album.state = AlbumState.INCOMPLETE
    _no_musicbrainz(monkeypatch)
    calls = []
    monkeypatch.setattr(retag_mod, "tag_with_release", lambda *a, **k: calls.append(k))

    _apply(tmp_path, [album])

    assert [(k["incomplete"], k["artwork_included"]) for k in calls] == [(True, False)]


def test_a_failed_write_is_reported_and_not_held(tmp_path, monkeypatch, caplog):
    album = _flagged(tmp_path, _catalogued())
    _no_musicbrainz(monkeypatch)

    def _disk_full(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(retag_mod, "tag_with_release", _disk_full)

    with caplog.at_level(logging.ERROR):
        result = _apply(tmp_path, [album])

    assert result is not None and (result.applied, result.failed) == (0, 1)
    assert auto_apply.held_reason(album) is None
    assert any(r.levelno == logging.ERROR and r.exc_info for r in caplog.records)
    finished = progress.status()["finished"]
    assert finished is not None
    assert finished["message"] == "0 albums updated, 1 couldn't be: see Activity"


def test_the_pass_shows_in_the_status_bar_and_says_what_it_did(tmp_path, monkeypatch):
    album = _flagged(tmp_path, _catalogued())
    _no_musicbrainz(monkeypatch)

    _apply(tmp_path, [album])

    finished = progress.status()["finished"]
    assert finished is not None
    assert (finished["label"], finished["message"]) == ("Applying updates", "1 album updated")


def test_a_selection_waits_for_no_pass_already_writing(tmp_path, monkeypatch):
    """The auto-apply pass may be working through the same albums."""
    album = _flagged(tmp_path, _catalogued())
    _no_musicbrainz(monkeypatch)
    started, release = threading.Event(), threading.Event()

    def _slow(*a, **k):
        started.set()
        release.wait(5)
        raise OSError("stopped")

    monkeypatch.setattr(retag_mod, "tag_with_release", _slow)
    cfg = _cfg(tmp_path, apply_up_to=S.ENRICHMENT)
    running = threading.Thread(
        target=auto_apply.apply_due, args=([album], cfg, PicardCompatibleTagger(), {})
    )
    running.start()
    try:
        assert started.wait(5)
        assert _apply(tmp_path, [album]) is None
    finally:
        release.set()
        running.join(5)


def test_an_identity_update_is_applied_whatever_the_threshold(tmp_path, monkeypatch):
    album = _flagged(tmp_path, _release("Test Album (remastered)"))
    _no_musicbrainz(monkeypatch)
    assert GardenerConfig().apply_up_to == "none"

    result = _apply(tmp_path, [album])

    assert result is not None and result.applied == 1
    # Under the album's new title, which is what the update wrote.
    assert [
        e.message for e in activity.recent(20) if "Test Album (remastered)" in (e.album_label or "")
    ] == ["Tag update applied (identity)"]


def test_no_musicbrainz_request_is_spent(tmp_path, monkeypatch):
    album = _flagged(tmp_path, _catalogued())
    calls = []
    monkeypatch.setattr(mb_lookup, "fetch_release", lambda *a, **k: calls.append(a))

    _apply(tmp_path, [album])

    assert calls == []


# ---- what the user sees while it runs ----------------------------------------


def test_each_card_knows_whether_it_waits_or_is_being_written(tmp_path, monkeypatch):
    album = _flagged(tmp_path, _catalogued())
    other = copy.copy(album)
    other.id = "another-album"
    _no_musicbrainz(monkeypatch)
    seen = []
    monkeypatch.setattr(
        retag_mod,
        "tag_with_release",
        lambda *a, **k: seen.append((auto_apply.queued(album), auto_apply.queued(other))),
    )

    _apply(tmp_path, [album, other])

    assert seen[0] == ("applying", "queued")
    assert (auto_apply.queued(album), auto_apply.queued(other)) == (None, None)


def test_a_card_is_no_longer_queued_when_the_inbox_refreshes_after_it(tmp_path, monkeypatch):
    album = _flagged(tmp_path, _catalogued())
    _no_musicbrainz(monkeypatch)
    at_refresh = []

    _apply(tmp_path, [album], on_processed=lambda a: at_refresh.append(auto_apply.queued(a)))

    assert at_refresh == [None]


def test_the_status_bar_names_the_album_and_its_track_while_writing(tmp_path, monkeypatch):
    album = _flagged(tmp_path, _catalogued())
    _no_musicbrainz(monkeypatch)
    seen = []
    original = formats.write_tags

    def writing(path, *args, **kwargs):
        seen.append(progress.status()["jobs"])
        return original(path, *args, **kwargs)

    monkeypatch.setattr(formats, "write_tags", writing)

    _apply(tmp_path, [album])

    assert seen[0] == [
        {
            "label": "Applying updates",
            "done": 0,
            "total": 1,
            "item": album.label,
            "foreground": True,
            "step": {"label": "Tagging", "done": 0, "total": len(seen)},
        }
    ]


# ---- the Inbox ------------------------------------------------------------------


def _inbox(web, monkeypatch, level=S.ENRICHMENT):  # noqa: F811
    """Two Library albums whose update waits in the Inbox at `level`."""
    cfg, client = web
    albums = _inbox_library(cfg, monkeypatch, 2, (_row("album", "Album", "Old", "New"),))
    for album in albums:
        album.update_significance = level
    return client, albums


def test_a_card_is_selected_with_the_version_it_was_drawn_at(web, monkeypatch):  # noqa: F811
    client, albums = _inbox(web, monkeypatch)

    page = client.get("/tasks").text

    for album in albums:
        assert f'name="album" value="{album.id}:v1"' in page
    assert 'hx-post="/updates/apply"' in page


def test_a_structure_update_cannot_be_selected(web, monkeypatch):  # noqa: F811
    client, _ = _inbox(web, monkeypatch, level=S.STRUCTURE)

    page = client.get("/tasks").text

    assert 'id="inbox-updates-structure"' in page
    assert 'name="album"' not in page


def test_while_a_selection_is_applied_its_cards_say_where_they_stand(web, monkeypatch):  # noqa: F811
    client, albums = _inbox(web, monkeypatch)
    first, second = albums
    assert auto_apply.claim(albums, {first.id: "v1", second.id: "v1"}) is not None
    try:
        auto_apply._queue[first.id] = "applying"
        page = client.get("/tasks").text
    finally:
        auto_apply._queue.clear()
        auto_apply._pass_lock.release()

    card = re.search(rf'<div id="task-{re.escape(second.id)}".*?</h3>', page, re.DOTALL)
    assert card and "update-card--queued" in card.group(0) and "Queued" in card.group(0)
    assert re.search(rf'value="{re.escape(second.id)}:v1"[^>]*disabled', page)
    assert "Applying…" in page
    # Nothing else can be applied meanwhile.
    assert not re.search(r"<button[^>]*update-group__apply", page)


# ---- the route ----------------------------------------------------------------


def _status(response) -> dict:
    return json.loads(response.headers["HX-Trigger"])["harmonist-status"]


def test_applying_a_selection_starts_the_pass_with_each_cards_version(tmp_path, monkeypatch):
    from harmonist.web import main as web_main
    from test.test_auto_apply import _client

    _, client = _client(tmp_path)
    started = []
    monkeypatch.setattr(web_main, "_start_bulk_apply", lambda app, chosen: started.append(chosen))

    response = client.post("/updates/apply", data={"album": ["rel-a:v1", "rel-b:v2"]})

    assert started == [{"rel-a": "v1", "rel-b": "v2"}]
    assert (_status(response)["verb"], _status(response)["details"]) == (
        "Applying updates",
        "2 albums selected",
    )


@pytest.mark.parametrize(
    ("form", "refused"),
    [
        ({"album": ["rel-a:v1"]}, None),
        ({"album": ["rel-a:v1"]}, "A sync or reconcile is running"),
        ({}, None),
    ],
    ids=["started", "refused", "nothing selected"],
)
def test_applying_a_selection_never_rescans_the_library(tmp_path, monkeypatch, form, refused):
    """The pass refreshes each album as it writes it. A full scan on top would
    lock the Inbox for as long as it takes, for one album's tags."""
    from harmonist.web import main as web_main
    from test.test_auto_apply import _client

    _, client = _client(tmp_path)
    monkeypatch.setattr(web_main, "_start_bulk_apply", lambda app, chosen: refused)
    scans = []
    monkeypatch.setattr(client.app.state.scan_runner, "request_scan", lambda: scans.append(1))

    client.post("/updates/apply", data=form)

    assert scans == []


def test_a_selection_is_not_applied_during_a_sync(tmp_path, monkeypatch):
    from test.test_auto_apply import _client

    _, client = _client(tmp_path)
    monkeypatch.setattr(type(client.app.state.sync_runner), "is_running", property(lambda s: True))
    claimed = []
    monkeypatch.setattr(auto_apply, "claim", lambda *a: claimed.append(a))

    response = client.post("/updates/apply", data={"album": ["rel-a:v1"]})

    assert claimed == []
    assert (_status(response)["verb"], _status(response)["level"]) == ("Not applied", "warning")


def test_nothing_selected_applies_nothing(tmp_path, monkeypatch):
    from harmonist.web import main as web_main
    from test.test_auto_apply import _client

    _, client = _client(tmp_path)
    started = []
    monkeypatch.setattr(web_main, "_start_bulk_apply", lambda app, chosen: started.append(chosen))

    response = client.post("/updates/apply", data={})

    assert started == []
    assert _status(response)["verb"] == "Nothing applied"
