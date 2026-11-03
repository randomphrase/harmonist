"""What each Inbox tag update would change, at a glance (#273 follow-up).

The update check already builds the album's whole plan to judge it, so it keeps
History's own rows for it beside the flag: the changes that count as an update,
the furthest-reaching first and, within a level, the fields a person reads
before the identifiers. The Inbox shows the first two.
"""

from __future__ import annotations

import copy
import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from harmonist import activity_store, gardener, mb_lookup, scanner, tag_history
from harmonist.config import (
    BandcampConfig,
    Config,
    GardenerConfig,
    PathsConfig,
    ServerConfig,
)
from harmonist.config import TestConfig as _TestConfig
from harmonist.formats import owned
from harmonist.web.main import create_app
from test.test_gardener import _release, _tagged
from test.test_web import _make_tagged_album

S = owned.Significance


def _flag(tmp_path, stored: dict):
    activity_store.init(tmp_path / "activity.db")
    album = _tagged(tmp_path, _release())
    activity_store.store_release("rel-aaa", "+".join(sorted(mb_lookup.RELEASE_INCLUDES)), stored)
    gardener.refresh_flag(album, stored)
    return album


def test_a_flagged_album_keeps_historys_rows_furthest_reaching_first(tmp_path):
    stored = copy.deepcopy(_release("Test Album (remastered)"))  # a retitle: Identity
    stored["label-info-list"] = [{"label": {"name": "Warp"}, "catalog-number": "WARPCD-1"}]

    album = _flag(tmp_path, stored)

    rows = album.update_changes
    assert all(isinstance(r, tag_history.FieldChange) for r in rows)
    album_row = album.update_lead[0]  # the retitle (Identity) leads the catalogue number
    assert (album_row.label, album_row.before, album_row.after) == (
        "Album",
        "Test Album",
        "Test Album (remastered)",
    )
    assert {"label", "catalog_number"} <= {r.field for r in rows}


def test_the_lead_puts_the_furthest_reaching_change_ahead_of_field_order(tmp_path):
    """A track title comes before the label in every change list, but a change
    to its casing only is Cosmetic; a label arriving is Enrichment, so it leads."""
    stored = copy.deepcopy(_release())
    stored["medium-list"][0]["track-list"][0]["title"] = "TRACK 1"
    stored["label-info-list"] = [{"label": {"name": "Warp"}, "catalog-number": "WARPCD-1"}]
    stored["barcode"] = "5051234567890"  # a fourth row, so the lead has to choose

    album = _flag(tmp_path, stored)

    assert album.update_changes[0].field == "title"
    assert [r.field for r in album.update_lead] == ["label", "catalog_number"]


def test_the_full_list_is_in_the_one_change_order(tmp_path):
    stored = copy.deepcopy(_release("Test Album (remastered)"))
    stored["barcode"] = "5051234567890"
    stored["medium-list"][0]["format"] = "CD"

    album = _flag(tmp_path, stored)

    fields = [r.field for r in album.update_changes]
    assert fields == ["album", "media", "barcode"]


def test_three_changes_lead_with_all_of_them_in_the_one_change_order(tmp_path):
    """A third row costs the line a Show all button would take, so a list of
    three is shown whole — in the order the full list uses, since nothing is
    left out for a ranking to choose between."""
    stored = copy.deepcopy(_release("Test Album (remastered)"))
    stored["barcode"] = "5051234567890"
    stored["medium-list"][0]["format"] = "CD"

    album = _flag(tmp_path, stored)

    assert album.update_lead == album.update_changes
    assert len(album.update_lead) == 3


def test_every_tag_field_has_one_place_in_the_change_order():
    """A field added to `Owned` without a place would sort after everything,
    identifiers included, with nothing to say it had been forgotten."""
    order = tag_history._FIELD_ORDER
    assert sorted(order, key=str) == sorted(owned.Owned, key=str)
    assert len(order) == len(set(order))


def test_a_tracklist_that_no_longer_fits_says_so_instead_of_listing_fields(tmp_path):
    """Two tracks on MusicBrainz for one file on disk: the plan is refused
    before any field diff exists, so there are no rows to show, and the reason
    is what the Inbox has to say."""
    album = _flag(tmp_path, _release(tracks=2))

    assert album.update_significance == S.STRUCTURE
    assert album.update_changes == ()
    note = "Track assignments need review · MusicBrainz lists 2 tracks, 1 on disk"
    assert album.update_note == note


def test_an_album_with_nothing_to_take_has_no_rows(tmp_path):
    album = _flag(tmp_path, _release())

    assert album.update_available is False
    assert album.update_changes == album.update_lead == ()
    assert album.update_note is None


@pytest.fixture
def web(tmp_path):
    cfg = Config(
        paths=PathsConfig(config_dir=tmp_path / "config", music_dir=tmp_path / "music"),
        bandcamp=BandcampConfig(),
        server=ServerConfig(),
        test=_TestConfig(mode="fixture"),
        gardener=GardenerConfig(review_from=S.SETTINGS),
    )
    cfg.paths.music_dir.mkdir(parents=True)
    cfg.paths.config_dir.mkdir(parents=True)
    return cfg, TestClient(create_app(cfg), headers={"HX-Request": "true"})


def _row(field, label, before, after):
    return tag_history.FieldChange(
        field=field,
        label=label,
        before=before,
        after=after,
        tracks=1,
        total=1,
        scope=owned.SCOPE[owned.Owned(field)],
    )


def _inbox_library(cfg, monkeypatch, count: int, changes: tuple):
    for i in range(count):
        _make_tagged_album(cfg, f"Album {i:02d}", mbid=f"rel-{i}", tagged_at=datetime.now(UTC))
    albums = scanner.scan(cfg.paths.music_dir)
    for album in albums:
        album.update_available = True
        album.update_significance = S.ENRICHMENT
        album.mb_version = "v1"
        album.update_changes = changes
        album.update_lead = changes[:2]
    monkeypatch.setattr(scanner, "scan", lambda *a, **k: albums)
    return albums


def _updates_section(client) -> str:
    match = re.search(
        r'<section[^>]*id="inbox-updates".*?</section>', client.get("/tasks").text, re.DOTALL
    )
    assert match
    return match.group(0)


def test_an_update_card_shows_its_first_two_changes_and_counts_the_rest(web, monkeypatch):
    cfg, client = web
    _inbox_library(
        cfg,
        monkeypatch,
        1,
        (
            _row("album", "Album", "Old", "New"),
            _row("date", "Date", "2020", "2020-05-01"),
            _row("label", "Label", None, "Warp"),
            _row("barcode", "Barcode", None, "5051234567890"),
        ),
    )

    lead, _, rest = _updates_section(client).partition('class="update-card__all')

    assert "<dt>Album</dt>" in lead and "<dt>Date</dt>" in lead
    assert "<dt>Label</dt>" not in lead
    # The full list, as History will show it, opened by the button beneath it.
    assert all(f"<dt>{label}</dt>" in rest for label in ("Album", "Date", "Label", "Barcode"))
    assert re.search(
        r'<button[^>]*class="update-card__more"[^>]*aria-expanded="false"[^>]*>'
        r"\s*<span[^>]*>Show all 4 changes</span>",
        rest,
    )


def test_an_update_cards_lead_shows_one_line_of_a_per_track_change(web, monkeypatch):
    """#820: the lead rows are a glimpse, so a field whose tracks changed
    differently shows its first track there and folds the rest; the full list
    beneath shows the first three, as History does."""
    titles = tag_history.FieldChange(
        field="title",
        label="Title",
        before=None,
        after=None,
        tracks=8,
        total=28,
        scope=owned.SCOPE[owned.Owned.TITLE],
        variants=tuple(
            tag_history.TrackChange(f"{i:02d}.m4a", str(i), f"Song {i}", f"Song {i} (mix)")
            for i in range(1, 9)
        ),
    )
    changes = (
        titles,
        _row("album", "Album", "Old", "New"),
        _row("date", "Date", "2020", "2020-05-01"),
        _row("label", "Label", None, "Warp"),
    )
    cfg, client = web
    _inbox_library(cfg, monkeypatch, 1, changes)

    lead, _, rest = _updates_section(client).partition('class="update-card__all')

    def shown(markup: str) -> int:
        dd = re.search(r"<dt>Title</dt>\s*<dd>(.*?)</dd>", markup, re.DOTALL)
        assert dd is not None
        return dd.group(1).partition("<details")[0].count('class="tag-changes__line"')

    assert (shown(lead), shown(rest)) == (1, 3)
    assert "<summary>and 7 more tracks</summary>" in lead
    assert "<summary>and 5 more tracks</summary>" in rest


def test_an_update_cards_title_is_an_in_app_link(web, monkeypatch):
    """#935: the title opens Harmonist's album page, so it is drawn as one."""
    cfg, client = web
    albums = _inbox_library(cfg, monkeypatch, 1, (_row("album", "Album", "Old", "New"),))

    link = re.search(rf'<a href="/album/{re.escape(albums[0].id)}"[^>]*>', _updates_section(client))

    assert link
    assert "app-link" in re.search(r'class="([^"]*)"', link.group(0)).group(1).split()


def test_an_update_card_carries_no_path_badges_or_format(web, monkeypatch):
    cfg, client = web
    albums = _inbox_library(cfg, monkeypatch, 1, (_row("album", "Album", "Old", "New"),))

    text = _updates_section(client)

    assert str(albums[0].path.relative_to(cfg.paths.music_dir)) not in text
    assert "MusicBrainz ↗" not in text and "musicbrainz.org/release" not in text


def test_a_long_update_group_is_never_collapsed(web, monkeypatch):
    cfg, client = web
    _inbox_library(cfg, monkeypatch, 15, (_row("album", "Album", "Old", "New"),))

    text = _updates_section(client)

    assert "Show all 15 albums" not in text
    assert text.count('id="task-') == 15
