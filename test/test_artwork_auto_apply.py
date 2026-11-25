"""Adding artwork nobody has to review (#853).

When the user lets Harmonist apply artwork additions, the apply pass writes them
without asking: from the cached archive image, never a request, and only the
additions of a plan that only adds. A plan that would replace anything, and an
album being watched for better artwork, wait for a person. What can't be
written is held, with its reason, in the Inbox.
"""

from __future__ import annotations

import re
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from harmonist import (
    activity,
    activity_store,
    album_artwork,
    artwork,
    artwork_updates,
    auto_apply,
    caa_cache,
    cover_art,
    formats,
    images,
    mb_lookup,
    progress,
    scanner,
)
from harmonist import sidecar as sidecar_mod
from harmonist.config import BandcampConfig, Config, GardenerConfig, PathsConfig, ServerConfig
from harmonist.config import TestConfig as _TestConfig
from harmonist.models import Album, ArtworkWatch
from harmonist.tagger import PicardCompatibleTagger
from test import test_web
from test.test_artwork import png_bytes
from test.test_gardener import _release, _tagged
from test.test_update_routing import _FORM

cfg = test_web.cfg
client = test_web.client

A = artwork.Operation
MBID = "rel-art"
FRONT = "https://example.test/front.png"
ARCHIVE = png_bytes(600, 600)


@pytest.fixture(autouse=True)
def _clean(tmp_path):
    activity_store.init(tmp_path / "activity.db")
    cover_art.configure_cache(tmp_path / "caa")
    caa_cache.configure(timedelta(days=7))
    auto_apply.reset()
    progress.reset()
    yield
    auto_apply.reset()
    progress.reset()
    cover_art.configure_cache(None)


def _routing(*, review="none", apply=A.ADDITION) -> GardenerConfig:
    return GardenerConfig(artwork_review_from=review, artwork_apply_up_to=apply)


def _config(root: Path, routing: GardenerConfig) -> Config:
    return Config(
        paths=PathsConfig(config_dir=root / "config", music_dir=root),
        bandcamp=BandcampConfig(),
        server=ServerConfig(),
        test=_TestConfig(mode="fixture"),
        gardener=routing,
    )


def _archive(data: bytes = ARCHIVE) -> None:
    """The archive's front, as the background check leaves it in the cache."""
    image = formats.EmbeddedArt.of(data, "image/png")
    assert image.size is not None
    cover_art.cache_image(MBID, data, "image/png")
    activity_store.store_cover_art(
        MBID,
        activity_store.CachedCoverArt(
            fetched_at=datetime.now(UTC),
            image_url=FRONT,
            width=image.size.width,
            height=image.size.height,
            mime="image/png",
            source="release",
        ),
    )


def _library(root: Path, routing: GardenerConfig, **albums: bytes | None) -> dict[str, Album]:
    """Tagged albums with no embedded art, each with the folder cover given,
    scanned and warmed as at startup, under `routing`."""
    for name, cover in albums.items():
        album = _tagged(root, _release(mbid=MBID), name=name)
        if cover is not None:
            (album.path / "cover.png").write_bytes(cover)
    artwork_updates.configure(lambda: artwork.FolderCoverPolicy.NEVER, lambda: routing)
    scanned = {a.path.name: a for a in scanner.scan(root)}
    artwork_updates.warm(list(scanned.values()))
    return scanned


def _rescan(root: Path, album: Album) -> Album:
    return next(a for a in scanner.scan(root) if a.path == album.path)


def _track(album: Album) -> Path:
    return next(album.path.glob("*.m4a"))


def _art(album: Album) -> str | None:
    cover = formats.read_cover(_track(album))
    return images.digest(cover[0]) if cover is not None else None


def _no_requests(monkeypatch) -> None:
    def refuse(*args, **kwargs):
        raise AssertionError("applying artwork asked a service")

    for name in ("check_front", "fetch_bytes", "fetch_listing", "fetch_image"):
        monkeypatch.setattr(cover_art, name, refuse)
    monkeypatch.setattr(mb_lookup, "fetch_release", refuse)


def _pass(root: Path, routing: GardenerConfig, albums) -> auto_apply.PassResult:
    return auto_apply.apply_due(list(albums), _config(root, routing), PicardCompatibleTagger(), {})


# ---- the threshold --------------------------------------------------------


@pytest.mark.parametrize(("threshold", "applied"), [(A.ADDITION, {A.ADDITION}), ("none", set())])
def test_only_additions_are_ever_applied(threshold, applied):
    assert {op for op in artwork_updates.ORDER if artwork_updates.applies(op, threshold)} == applied


def test_nothing_is_applied_unless_told_otherwise():
    assert GardenerConfig().artwork_apply_up_to == "none"


def test_a_replacement_threshold_is_refused():
    with pytest.raises(ValueError, match="replacements are never applied"):
        GardenerConfig(artwork_apply_up_to=A.REPLACEMENT)


def test_the_artwork_apply_threshold_can_be_set_from_the_environment(tmp_path, monkeypatch):
    from harmonist import config

    monkeypatch.setenv("HARMONIST_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HARMONIST_MUSIC_DIR", str(tmp_path / "music"))
    monkeypatch.setenv("HARMONIST_GARDENER_ARTWORK_APPLY_UP_TO", "addition")
    assert config.load().gardener.artwork_apply_up_to == A.ADDITION


@pytest.mark.parametrize(
    ("applied", "held", "added", "line"),
    [
        (0, 0, 1, "Artwork added to 1 album"),
        (1, 1, 2, "1 album updated, artwork added to 2 albums, 1 held for review"),
        (2, 0, 0, "2 albums updated"),
    ],
)
def test_the_status_bar_says_what_the_pass_did(applied, held, added, line):
    assert auto_apply._summary(applied, held, added) == line


# ---- the pass -------------------------------------------------------------


def test_an_addition_is_written_from_the_cache_as_its_own_undoable_action(tmp_path, monkeypatch):
    _archive()
    routing = _routing()
    bare = _library(tmp_path, routing, Bare=None)["Bare"]
    _no_requests(monkeypatch)

    result = _pass(tmp_path, routing, [bare])

    assert result.artwork == 1
    assert _art(bare) == images.digest(ARCHIVE)
    entries = [e for e in activity.recent(10) if e.album_id == bare.id]
    assert [e.message for e in entries] == ["Artwork added automatically to 1 file"]
    # Its own action, which History offers Undo for: the write's audit record
    # carries the same id as the entry.
    assert entries[0].action_id is not None
    audit = activity_store.audit_by_action([entries[0].action_id])[entries[0].action_id]
    assert any(r.message.startswith("artwork.update") for r in audit)


def test_a_second_pass_finds_nothing_and_says_nothing(tmp_path):
    _archive()
    routing = _routing()
    root = tmp_path
    bare = _library(root, routing, Bare=None)["Bare"]
    assert _pass(root, routing, [bare]).artwork == 1
    count = len(activity.recent(50))

    again = _pass(root, routing, [_rescan(root, bare)])

    assert again.artwork == 0
    assert len(activity.recent(50)) == count


def test_a_plan_that_would_also_replace_is_left_whole_for_a_person(tmp_path):
    _archive()
    routing = _routing(review=A.REPLACEMENT)
    small = png_bytes(100, 100)
    album = _library(tmp_path, routing, Small=small)["Small"]
    assert artwork_updates.operation(album) is A.REPLACEMENT

    assert _pass(tmp_path, routing, [album]).artwork == 0

    assert _art(album) is None  # its track's addition is not taken either
    assert (album.path / "cover.png").read_bytes() == small
    assert not auto_apply.is_artwork_held(album)
    assert artwork_updates.for_review([album], routing, auto_apply.is_artwork_held) == [album]


def test_a_watched_album_is_shown_not_written(tmp_path):
    _archive()
    routing = _routing()
    bare = _library(tmp_path, routing, Bare=None)["Bare"]
    sc = sidecar_mod.read(bare.path)
    assert sc is not None
    sidecar_mod.write(bare.path, replace(sc, artwork_watch=ArtworkWatch(MBID)))
    watched = _rescan(tmp_path, bare)

    assert _pass(tmp_path, routing, [watched]).artwork == 0

    assert _art(watched) is None
    assert artwork_updates.for_review([watched], routing, auto_apply.is_artwork_held) == [watched]


def test_files_changed_since_the_scan_are_judged_as_they_are_now(tmp_path):
    _archive()
    routing = _routing()
    bare = _library(tmp_path, routing, Bare=None)["Bare"]
    # Larger than the archive's: a smaller one would now be a replacement on
    # this lossless album (#872), and held as one.
    mine = png_bytes(700, 700)
    formats.write_cover(_track(bare), mine)  # after the scan that routed it

    assert _pass(tmp_path, routing, [bare]).artwork == 0

    assert _art(bare) == images.digest(mine)
    assert not auto_apply.is_artwork_held(bare)
    assert not [e for e in activity.recent(10) if e.album_id == bare.id]


def test_a_folder_cover_added_since_the_scan_turns_it_into_a_replacement(tmp_path):
    _archive()
    routing = _routing()
    bare = _library(tmp_path, routing, Bare=None)["Bare"]
    assert artwork_updates.operation(bare) is A.ADDITION
    small = png_bytes(100, 100)
    (bare.path / "cover.png").write_bytes(small)  # after the scan that routed it

    assert _pass(tmp_path, routing, [bare]).artwork == 0

    assert (bare.path / "cover.png").read_bytes() == small
    assert _art(bare) is None
    assert auto_apply.artwork_held_reason(bare) == (
        "It would now replace artwork, so it is applied from its page"
    )


def test_a_watch_started_after_the_pass_chose_it_still_stops_the_write(tmp_path):
    _archive()
    routing = _routing()
    bare = _library(tmp_path, routing, Bare=None)["Bare"]
    assert bare.sidecar is not None
    watched = replace(bare, sidecar=replace(bare.sidecar, artwork_watch=ArtworkWatch(MBID)))
    assert artwork_updates.due([watched], A.ADDITION, held=lambda _: False) == []

    assert not auto_apply._apply_artwork_one(watched, _config(tmp_path, routing))

    assert _art(watched) is None


def test_a_failed_write_is_held_with_its_reason_until_the_plan_moves(tmp_path, monkeypatch):
    _archive()
    routing = _routing()
    bare = _library(tmp_path, routing, Bare=None)["Bare"]
    attempts = []

    def fail(*args, **kwargs):
        attempts.append(args)
        raise OSError("disk full")

    monkeypatch.setattr(album_artwork, "apply", fail)

    assert _pass(tmp_path, routing, [bare]) == auto_apply.PassResult(applied=0, held=1, artwork=0)
    assert auto_apply.artwork_held_reason(bare) == "Couldn't be written: disk full"
    assert artwork_updates.for_review([bare], routing, auto_apply.is_artwork_held) == [bare]

    # Not retried while nothing has changed…
    _pass(tmp_path, routing, [bare])
    assert len(attempts) == 1

    # …and retried once the archive's answer changes what it would write.
    _archive(png_bytes(700, 700))
    artwork_updates.observe(MBID)
    assert auto_apply.artwork_held_reason(bare) is None
    _pass(tmp_path, routing, [bare])
    assert len(attempts) == 2


def test_an_album_whose_tags_the_pass_writes_waits_for_the_next_pass(tmp_path, monkeypatch):
    _archive()
    routing = _routing()
    bare = _library(tmp_path, routing, Bare=None)["Bare"]
    monkeypatch.setattr(auto_apply, "due", lambda albums, ignored, apply_up_to: list(albums))
    monkeypatch.setattr(auto_apply, "_apply_one", lambda album, cfg, tagger: (True, album))

    result = _pass(tmp_path, routing, [bare])

    assert (result.applied, result.artwork) == (1, 0)
    assert _art(bare) is None


# ---- starting a pass ------------------------------------------------------


@pytest.mark.parametrize(
    ("routing", "starts"),
    [(_routing(), True), (_routing(apply="none"), False)],
)
def test_an_artwork_threshold_alone_starts_the_pass(tmp_path, routing, starts):
    from harmonist.web.main import _auto_apply_if_idle

    app = Mock(
        state=SimpleNamespace(cfg=_config(tmp_path, routing), tagger=PicardCompatibleTagger())
    )
    idle = Mock(is_running=False)
    runner = Mock(has_completed=lambda: True, albums=list)

    reason = _auto_apply_if_idle(app, idle, idle, runner)

    for worker in threading.enumerate():
        if worker.name == "harmonist-auto-apply":
            worker.join(5)
    assert (reason is None) == starts


# ---- Settings and the Inbox -----------------------------------------------


def test_saving_the_artwork_apply_tick_keeps_it_and_starts_applying(client, monkeypatch):
    from harmonist.web import main as web_main

    started = []
    monkeypatch.setattr(web_main, "_auto_apply_if_idle", lambda *a, **k: started.append(a))
    form = {
        **_FORM,
        "gardener_artwork_routing": "1",
        "gardener_artwork_apply": ["addition"],
        "gardener_artwork_review": ["replacement"],
    }

    assert client.post("/settings", data=form).status_code == 200

    routing = client.app.state.cfg.gardener
    assert (routing.artwork_apply_up_to, routing.artwork_review_from) == (
        A.ADDITION,
        A.REPLACEMENT,
    )
    assert len(started) == 1
    html = client.get("/settings").text
    box = re.search(r'<input[^>]*name="gardener_artwork_apply"[^>]*>', html)
    assert box is not None and "checked" in re.sub(r'"[^"]*"', '""', box.group(0))


def test_settings_refuses_to_apply_replacements(client):
    """The grid never offers it, so only a hand-built request can ask."""
    form = {**_FORM, "gardener_artwork_routing": "1", "gardener_artwork_apply": ["replacement"]}

    response = client.post("/settings", data=form)

    assert "Couldn't save" in response.text
    assert client.app.state.cfg.gardener.artwork_apply_up_to == "none"


def test_a_held_addition_waits_in_the_inbox_with_its_reason(client, cfg, monkeypatch):
    _archive()
    routing = _routing()
    client.app.state.cfg = client.app.state.cfg.model_copy(update={"gardener": routing})
    bare = _library(cfg.paths.music_dir, routing, Bare=None)["Bare"]
    monkeypatch.setattr(album_artwork, "apply", Mock(side_effect=OSError("disk full")))
    _pass(cfg.paths.music_dir, routing, [bare])

    html = client.get("/tasks").text

    card = re.search(
        rf'<div id="task-artwork-{re.escape(bare.id)}".*?(?=<div id="task-artwork-|</section>)',
        html,
        re.DOTALL,
    )
    assert card is not None
    assert "Not applied automatically: Couldn&#39;t be written: disk full" in card.group(0)
