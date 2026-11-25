"""Naming the MusicBrainz IDs in tag change lists (#801).

An identifier row says nothing a person can act on as two UUIDs. The album's
stored release names most of them, so the row shows names linked to
MusicBrainz, and a shortened ID where the release doesn't name one.
"""

from __future__ import annotations

import copy
import re

import pytest

from harmonist import activity_store, gardener, mb_lookup, tag_history, tagger
from harmonist.config import BandcampConfig, Config, PathsConfig, ServerConfig
from harmonist.config import TestConfig as _TestConfig
from harmonist.web.main import create_app
from test.test_gardener import _release, _tagged

OLD = "af540162-fea4-441a-b228-9008d3b5b41c"


def _released() -> dict:
    release = copy.deepcopy(_release())
    release["disambiguation"] = "deluxe"
    release["release-group"]["title"] = "Test Album"
    release["medium-list"][0]["track-list"][0]["artist-credit"] = [
        {"artist": {"id": "art-bbb", "name": "Guest"}}
    ]
    return release


def test_a_release_names_its_own_ids():
    names = tagger.change_names(_released())

    assert names["rel-aaa"] == "Test Album (deluxe)"
    assert names["rg-aaa"] == "Test Album"
    assert names["art-aaa"] == "Test Artist"
    assert names["art-bbb"] == "Guest"
    assert names["rec-001"] == "Track 1"
    assert names["rt-001"] == "Track 1"


def _id_row(before: str | None, after: str | None, field: str = "mb_track_id"):
    return tag_history.FieldChange(
        field=field,
        label=tag_history.label_for(field),
        before=before,
        after=after,
        tracks=1,
        total=1,
    )


def test_an_id_row_names_what_the_release_knows_and_shortens_the_rest():
    (row,) = tag_history.named((_id_row(OLD, "rec-001"),), {"rec-001": "Track 1"})

    after = row.refs(row.after)
    before = row.refs(row.before)
    assert [(r.label, r.url, r.id) for r in after] == [
        ("Track 1", "https://musicbrainz.org/recording/rec-001", "rec-001")
    ]
    assert [r.label for r in before] == ["af540162…"]


def test_an_id_rows_separators_are_kept_so_a_regrouping_still_shows():
    """One slash-joined value becoming two values is a real change; naming the
    ids must not make both sides read the same."""
    (row,) = tag_history.named(
        (_id_row("art-aaa/art-bbb", "art-aaa; art-bbb", "mb_artist_ids"),),
        {"art-aaa": "Test Artist", "art-bbb": "Guest"},
    )

    assert [p if isinstance(p, str) else p.label for p in row.parts(row.before)] == [
        "Test Artist",
        "/",
        "Guest",
    ]
    assert [p if isinstance(p, str) else p.label for p in row.parts(row.after)] == [
        "Test Artist",
        "; ",
        "Guest",
    ]


def test_other_rows_are_left_as_they_are():
    row = tag_history.FieldChange(
        field="album", label="Album", before="Old", after="New", tracks=1, total=1
    )

    assert tag_history.named((row,), {"Old": "x"}) == (row,)
    assert not row.is_id


def test_an_id_row_carries_no_character_diff(tmp_path):
    activity_store.init(tmp_path / "activity.db")
    album = _tagged(tmp_path, _release())
    stored = copy.deepcopy(_release())
    stored["medium-list"][0]["track-list"][0]["recording"]["id"] = "rec-new"
    activity_store.store_release("rel-aaa", "+".join(sorted(mb_lookup.RELEASE_INCLUDES)), stored)

    album = gardener.refresh_flag(album, stored).album

    row = next(r for r in album.update_changes if r.field == "mb_track_id")
    assert row.before_runs == row.after_runs == ()
    assert [r.label for r in row.refs(row.after)] == ["Track 1"]
    # The old recording is no longer on the stored release, so it can't be named.
    assert [r.label for r in row.refs(row.before)] == ["rec-001…"]


@pytest.fixture
def templates(tmp_path):
    cfg = Config(
        paths=PathsConfig(config_dir=tmp_path / "config", music_dir=tmp_path / "music"),
        bandcamp=BandcampConfig(),
        server=ServerConfig(),
        test=_TestConfig(mode="fixture"),
    )
    cfg.paths.music_dir.mkdir(parents=True)
    cfg.paths.config_dir.mkdir(parents=True)
    return create_app(cfg).state.templates.env


def test_the_change_list_links_each_named_id_to_musicbrainz(templates):
    (row,) = tag_history.named((_id_row(OLD, "rec-001"),), {"rec-001": "Track 1"})

    html = templates.get_template("partials/_tag_changes.html").render(changes=(row,))

    assert re.search(
        r'<a href="https://musicbrainz.org/recording/rec-001"[^>]*title="rec-001"[^>]*>Track 1</a>',
        html,
    )
    assert re.search(rf'<a href="https://musicbrainz.org/recording/{OLD}"[^>]*>af540162…</a>', html)
