"""Applying tag updates nobody has to review (#273, slice 2).

The user picks the highest significance Harmonist may apply by itself; that
level and every level below it are written without asking, Structure never.
What cannot be applied unattended is held, with its reason, in the Inbox.
"""

from __future__ import annotations

import copy
import logging

import pytest

from harmonist import activity, activity_store, auto_apply, formats, gardener, mb_lookup, progress
from harmonist import retag as retag_mod
from harmonist.config import BandcampConfig, Config, GardenerConfig, PathsConfig, ServerConfig
from harmonist.config import TestConfig as _TestConfig
from harmonist.formats import owned
from harmonist.models import AlbumState
from harmonist.tagger import PicardCompatibleTagger
from test.test_gardener import _release, _tagged

S = owned.Significance


@pytest.fixture(autouse=True)
def _clean():
    auto_apply.reset()
    progress.reset()
    yield
    auto_apply.reset()
    progress.reset()


def _cfg(tmp_path, *, apply_up_to="none"):
    return Config(
        paths=PathsConfig(config_dir=tmp_path / "config", music_dir=tmp_path),
        bandcamp=BandcampConfig(),
        server=ServerConfig(),
        test=_TestConfig(mode="fixture"),
        gardener=GardenerConfig(apply_up_to=apply_up_to),
    )


def _catalogued() -> dict:
    """The release with a catalogue number MusicBrainz has since been given: an
    Enrichment update."""
    release = copy.deepcopy(_release())
    release["label-info-list"] = [{"label": {"name": "Warp"}, "catalog-number": "WARPCD-999"}]
    return release


def _flagged(tmp_path, stored: dict):
    """An album tagged from the original release, with `stored` as what
    MusicBrainz says now, flagged as the update check would flag it."""
    activity_store.init(tmp_path / "activity.db")
    album = _tagged(tmp_path, _release())
    activity_store.store_release("rel-aaa", "+".join(sorted(mb_lookup.RELEASE_INCLUDES)), stored)
    album = gardener.refresh_flag(album, stored).album
    assert album.update_available
    return album


def _no_musicbrainz(monkeypatch):
    def _refuse(*a, **k):
        raise AssertionError("applying an update asked MusicBrainz")

    monkeypatch.setattr(mb_lookup, "fetch_release", _refuse)
    monkeypatch.setattr(mb_lookup, "fetch_release_urls", _refuse)


def _catalogue_number(album) -> str | None:
    """The catalogue number the file carries; the tag holds a list."""
    values = formats.read_owned(next(album.path.glob("*.m4a"))).get("catalog_number") or []
    return values[0] if values else None


# ---- the threshold --------------------------------------------------------


@pytest.mark.parametrize("threshold", [*owned.ORDER, "none"])
def test_harmonist_applies_the_chosen_level_and_every_level_below_it(threshold):
    applied = {level for level in owned.ORDER if gardener.applies(level, threshold)}
    if threshold == "none":
        expected = set()
    else:
        expected = set(owned.ORDER[: owned.ORDER.index(threshold) + 1]) - {S.STRUCTURE}
    assert applied == expected


def test_nothing_applies_itself_unless_told_to():
    assert GardenerConfig().apply_up_to == "none"


def test_structure_can_never_be_applied_automatically():
    with pytest.raises(ValueError, match="structure"):
        GardenerConfig.model_validate({"apply_up_to": "structure"})


def test_the_apply_level_can_be_set_from_the_environment(tmp_path, monkeypatch):
    from harmonist import config

    monkeypatch.setenv("HARMONIST_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HARMONIST_MUSIC_DIR", str(tmp_path / "music"))
    monkeypatch.setenv("HARMONIST_GARDENER_APPLY_UP_TO", "enrichment")
    assert config.load().gardener.apply_up_to == S.ENRICHMENT


# ---- the pass ---------------------------------------------------------------


def test_an_update_in_range_is_written_from_the_stored_release(tmp_path, monkeypatch):
    album = _flagged(tmp_path, _catalogued())
    _no_musicbrainz(monkeypatch)

    result = auto_apply.apply_due(
        [album], _cfg(tmp_path, apply_up_to=S.ENRICHMENT), PicardCompatibleTagger(), {}
    )

    assert (result.applied, result.held) == (1, 0)
    assert _catalogue_number(album) == "WARPCD-999"
    album = gardener.refresh_flag(album, _catalogued()).album
    assert album.update_available is False


def test_an_applied_update_is_recorded_as_one_undoable_action(tmp_path, monkeypatch):
    album = _flagged(tmp_path, _catalogued())
    _no_musicbrainz(monkeypatch)

    auto_apply.apply_due(
        [album], _cfg(tmp_path, apply_up_to=S.ENRICHMENT), PicardCompatibleTagger(), {}
    )

    entries = [e for e in activity.recent(10) if e.album_label == album.label]
    assert [e.message for e in entries] == ["Tag update applied automatically (enrichment)"]
    assert entries[0].action_id is not None
    audit = activity_store.audit_by_action([entries[0].action_id])[entries[0].action_id]
    assert any(e.message.startswith("tag.album") for e in audit)


def test_a_second_pass_after_applying_is_a_no_op(tmp_path, monkeypatch):
    album = _flagged(tmp_path, _catalogued())
    _no_musicbrainz(monkeypatch)
    cfg = _cfg(tmp_path, apply_up_to=S.ENRICHMENT)

    auto_apply.apply_due([album], cfg, PicardCompatibleTagger(), {})
    entries = len(activity.recent(50, include_audit=True))
    again = auto_apply.apply_due([album], cfg, PicardCompatibleTagger(), {})

    assert (again.applied, again.held) == (0, 0)
    assert len(activity.recent(50, include_audit=True)) == entries


def test_an_update_beyond_the_range_is_left_alone(tmp_path, monkeypatch):
    album = _flagged(tmp_path, _release("Test Album (remastered)"))  # a retitle: Identity
    _no_musicbrainz(monkeypatch)

    result = auto_apply.apply_due(
        [album], _cfg(tmp_path, apply_up_to=S.ENRICHMENT), PicardCompatibleTagger(), {}
    )

    assert (result.applied, result.held) == (0, 0)
    assert album.update_available is True
    assert auto_apply.held_reason(album) is None


def test_an_ignored_update_is_never_applied(tmp_path, monkeypatch):
    album = _flagged(tmp_path, _catalogued())
    _no_musicbrainz(monkeypatch)
    activity_store.ignore_update(album.id, release_version=album.mb_version or "")
    ignored = activity_store.ignored_updates()

    result = auto_apply.apply_due(
        [album], _cfg(tmp_path, apply_up_to=S.ENRICHMENT), PicardCompatibleTagger(), ignored
    )

    assert result.applied == 0
    assert _catalogue_number(album) is None


def test_an_update_that_reaches_further_by_write_time_is_held(tmp_path, monkeypatch):
    """Flagged as Enrichment, but MusicBrainz has since retitled the album: the
    write re-judges, finds Identity, and holds it rather than writing a change
    nobody agreed to have applied."""
    album = _flagged(tmp_path, _catalogued())
    retitled = {**_catalogued(), "title": "Test Album (remastered)"}
    activity_store.store_release("rel-aaa", "+".join(sorted(mb_lookup.RELEASE_INCLUDES)), retitled)
    _no_musicbrainz(monkeypatch)

    result = auto_apply.apply_due(
        [album], _cfg(tmp_path, apply_up_to=S.ENRICHMENT), PicardCompatibleTagger(), {}
    )

    assert (result.applied, result.held) == (0, 1)
    assert _catalogue_number(album) is None
    album = gardener.refresh_flag(album, retitled).album
    assert auto_apply.held_reason(album) == "Its update now reaches identity"


def test_an_incomplete_album_is_held_for_its_page(tmp_path, monkeypatch):
    album = _flagged(tmp_path, _catalogued())
    album.state = AlbumState.INCOMPLETE
    _no_musicbrainz(monkeypatch)

    result = auto_apply.apply_due(
        [album], _cfg(tmp_path, apply_up_to=S.ENRICHMENT), PicardCompatibleTagger(), {}
    )

    assert result.held == 1
    assert auto_apply.held_reason(album) == "Tracks are missing, so it is applied from its page"


def test_a_failed_write_is_held_and_logged_loudly(tmp_path, monkeypatch, caplog):
    album = _flagged(tmp_path, _catalogued())
    _no_musicbrainz(monkeypatch)

    def _disk_full(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(retag_mod, "tag_with_release", _disk_full)

    with caplog.at_level(logging.ERROR):
        result = auto_apply.apply_due(
            [album], _cfg(tmp_path, apply_up_to=S.ENRICHMENT), PicardCompatibleTagger(), {}
        )

    assert result.held == 1
    assert auto_apply.held_reason(album) == "Couldn't be written: disk full"
    assert any(r.levelno == logging.ERROR and r.exc_info for r in caplog.records)


def test_a_held_update_is_not_retried_until_musicbrainz_moves(tmp_path, monkeypatch):
    album = _flagged(tmp_path, _catalogued())
    _no_musicbrainz(monkeypatch)
    calls = []

    def _disk_full(*a, **k):
        calls.append(1)
        raise OSError("disk full")

    monkeypatch.setattr(retag_mod, "tag_with_release", _disk_full)
    cfg = _cfg(tmp_path, apply_up_to=S.ENRICHMENT)

    auto_apply.apply_due([album], cfg, PicardCompatibleTagger(), {})
    auto_apply.apply_due([album], cfg, PicardCompatibleTagger(), {})
    assert len(calls) == 1

    album.mb_version = "moved"
    auto_apply.apply_due([album], cfg, PicardCompatibleTagger(), {})
    assert len(calls) == 2


def test_the_pass_shows_in_the_status_bar_and_says_what_it_did(tmp_path, monkeypatch):
    album = _flagged(tmp_path, _catalogued())
    _no_musicbrainz(monkeypatch)

    auto_apply.apply_due(
        [album], _cfg(tmp_path, apply_up_to=S.ENRICHMENT), PicardCompatibleTagger(), {}
    )

    finished = progress.status()["finished"]
    assert finished is not None
    assert (finished["label"], finished["message"]) == ("Applying updates", "1 album updated")


def test_the_pass_names_the_album_it_is_writing_with_its_tracks(tmp_path, monkeypatch):
    album = _flagged(tmp_path, _catalogued())
    _no_musicbrainz(monkeypatch)
    seen = []
    original = formats.write_tags

    def writing(path, *args, **kwargs):
        seen.append(progress.status()["jobs"])
        return original(path, *args, **kwargs)

    monkeypatch.setattr(formats, "write_tags", writing)

    auto_apply.apply_due(
        [album], _cfg(tmp_path, apply_up_to=S.ENRICHMENT), PicardCompatibleTagger(), {}
    )

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


# ---- routing ----------------------------------------------------------------


def test_a_held_update_waits_in_the_inbox_whatever_the_review_level(tmp_path):
    album = _flagged(tmp_path, _catalogued())
    album.update_significance = S.ENRICHMENT
    other = copy.copy(album)

    routed = gardener.for_review(
        [album],
        {},
        GardenerConfig(apply_up_to=S.ENRICHMENT, review_from=S.STRUCTURE),
        held=lambda a: a is album,
    )
    not_held = gardener.for_review(
        [other],
        {},
        GardenerConfig(apply_up_to=S.ENRICHMENT, review_from=S.SETTINGS),
        held=lambda a: False,
    )

    assert routed == [album]
    assert not_held == []  # it will be applied; reviewing it as well would be asking twice


def _client(tmp_path):
    from fastapi.testclient import TestClient

    from harmonist.web.main import create_app

    cfg = Config(
        paths=PathsConfig(config_dir=tmp_path / "config", music_dir=tmp_path / "music"),
        bandcamp=BandcampConfig(),
        server=ServerConfig(),
        test=_TestConfig(mode="fixture"),
    )
    cfg.paths.music_dir.mkdir(parents=True)
    cfg.paths.config_dir.mkdir(parents=True)
    return cfg, TestClient(create_app(cfg), headers={"HX-Request": "true"})


_FORM = {
    "download_format": "flac",
    "max_downloads_per_sync": "10",
    "user_agent": "Harmonist/test ( test@example.com )",
    "gardener_level": "off",
    "folder_cover": "never",
    "standardize_artist_names": "none",
    "log_level": "info",
}


def test_saving_an_apply_level_keeps_it_and_starts_applying(tmp_path, monkeypatch):
    from harmonist.web import main as web_main

    _, client = _client(tmp_path)
    started = []
    monkeypatch.setattr(web_main, "_auto_apply_if_idle", lambda *a, **k: started.append(a))

    response = client.post(
        "/settings",
        data={
            **_FORM,
            "gardener_routing": "1",
            "gardener_apply": ["enrichment", "cosmetic", "settings"],
            "gardener_review": ["structure"],
        },
    )

    assert response.status_code == 200
    assert client.app.state.cfg.gardener.apply_up_to == S.ENRICHMENT
    assert len(started) == 1
    toml = (tmp_path / "config" / "harmonist.toml").read_text()
    assert 'apply_up_to = "enrichment"' in toml


def test_settings_refuses_to_apply_structure(tmp_path):
    """The grid never offers it, so only a hand-built request can ask."""
    _, client = _client(tmp_path)

    response = client.post(
        "/settings", data={**_FORM, "gardener_routing": "1", "gardener_apply": ["structure"]}
    )

    assert "Couldn't save" in response.text
    assert client.app.state.cfg.gardener.apply_up_to == "none"


def test_a_held_update_shows_its_reason_in_the_inbox(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from harmonist import scanner
    from test.test_web import _make_tagged_album

    cfg, client = _client(tmp_path)
    client.app.state.cfg = cfg.model_copy(
        update={"gardener": GardenerConfig(apply_up_to=S.ENRICHMENT)}
    )
    _make_tagged_album(cfg, "Stuck", mbid="rel-stuck", tagged_at=datetime.now(UTC))
    albums = scanner.scan(cfg.paths.music_dir)
    for album in albums:
        album.update_available = True
        album.update_significance = S.ENRICHMENT
        album.mb_version = "v1"
    monkeypatch.setattr(scanner, "scan", lambda *a, **k: albums)
    auto_apply._hold(albums[0], "Track assignments need review")

    page = client.get("/tasks").text

    assert 'id="inbox-updates-enrichment"' in page
    assert "Track assignments need review" in page


def test_the_digest_leaves_out_updates_that_will_apply_themselves(tmp_path):
    album = _flagged(tmp_path, _catalogued())
    album.update_significance = S.ENRICHMENT
    gardener.configure_routing(
        lambda: GardenerConfig(apply_up_to=S.ENRICHMENT, review_from=S.SETTINGS)
    )
    activity_store.clear()

    gardener._record_digest(
        gardener.PassResult(
            asked=1,
            assessments=(gardener.Assessment(None, None, album),),
            newly_flagged=(album,),
            gone=0,
            failed=0,
        )
    )

    assert all(e.album_label != album.label for e in activity.recent(10))
