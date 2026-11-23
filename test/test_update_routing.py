"""Where an album's pending update is shown, by how far it reaches (#273).

The user picks the lowest significance the Inbox takes; that level and every
level above it go to the Inbox, everything below stays findable through the
Library's update filter only. The order is `owned.ORDER`, so a threshold can
never route a level more cautiously than one above it.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from harmonist import activity, activity_store, gardener, scanner
from harmonist.config import BandcampConfig, Config, GardenerConfig, PathsConfig, ServerConfig
from harmonist.config import TestConfig as _TestConfig
from harmonist.formats import owned
from harmonist.models import Album
from harmonist.web.main import create_app
from test.test_web import _make_album, _make_tagged_album

S = owned.Significance


@pytest.fixture
def cfg(tmp_path):
    return Config(
        paths=PathsConfig(config_dir=tmp_path / "config", music_dir=tmp_path / "music"),
        bandcamp=BandcampConfig(),
        server=ServerConfig(),
        test=_TestConfig(mode="fixture"),
    )


@pytest.fixture
def client(cfg):
    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.config_dir.mkdir(parents=True, exist_ok=True)
    return TestClient(create_app(cfg), headers={"HX-Request": "true"})


def _flagged_library(cfg, monkeypatch, levels: dict[str, owned.Significance | None]) -> list[Album]:
    """A tagged album per name, each flagged at its level, served to every
    request: without the lifespan, each request rescans and would drop the
    in-memory flags the update check sets."""
    for i, name in enumerate(levels):
        _make_tagged_album(cfg, name, mbid=f"rel-{i}", tagged_at=datetime.now(UTC))
    albums = scanner.scan(cfg.paths.music_dir)
    for album in albums:
        level = levels[album.path.name]
        album.update_available = level is not None
        album.update_significance = level
        album.mb_version = "v1" if level is not None else None
    monkeypatch.setattr(scanner, "scan", lambda *args, **kwargs: albums)
    return albums


def _updates_section(html: str) -> str:
    match = re.search(r'<section[^>]*id="inbox-updates".*?</section>', html, re.DOTALL)
    return match.group(0) if match else ""


def _headings(section: str) -> list[str]:
    return re.findall(r'id="inbox-updates-(\w+)"', section)


# ---- the threshold itself -------------------------------------------------


@pytest.mark.parametrize("threshold", [*owned.ORDER, "none"])
def test_the_inbox_takes_the_chosen_level_and_every_level_above_it(threshold):
    routed = {level for level in owned.ORDER if gardener.reviews(level, threshold)}
    expected = set() if threshold == "none" else set(owned.ORDER[owned.ORDER.index(threshold) :])
    assert routed == expected


def test_the_library_lists_update_levels_in_the_order_the_threshold_uses():
    from harmonist.web.main import _LIBRARY_UPDATE_LEVELS

    assert list(_LIBRARY_UPDATE_LEVELS.values()) == list(reversed(owned.ORDER))


def test_the_inbox_starts_at_structure_unless_told_otherwise():
    assert GardenerConfig().review_from == S.STRUCTURE


def test_an_unknown_review_level_is_refused_with_the_levels_named():
    with pytest.raises(ValueError, match="enrichment"):
        GardenerConfig.model_validate({"review_from": "everything"})


def test_the_review_level_can_be_set_from_the_environment(tmp_path, monkeypatch):
    from harmonist import config

    monkeypatch.setenv("HARMONIST_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HARMONIST_MUSIC_DIR", str(tmp_path / "music"))
    monkeypatch.setenv("HARMONIST_GARDENER_REVIEW_FROM", "identity")
    assert config.load().gardener.review_from == S.IDENTITY


# ---- the Log threshold (#1024) --------------------------------------------


@pytest.mark.parametrize("threshold", [*owned.ORDER, "none"])
def test_activity_records_the_chosen_level_and_every_level_above_it(threshold):
    recorded = {level for level in owned.ORDER if gardener.records(level, threshold)}
    expected = set() if threshold == "none" else set(owned.ORDER[owned.ORDER.index(threshold) :])
    assert recorded == expected


def test_activity_records_every_update_from_musicbrainz_unless_told_otherwise():
    """Settings is the user's to add: it brings an album into line with a
    setting they chose, so it is the one level off by default."""
    assert GardenerConfig().activity_from == S.COSMETIC
    assert GardenerConfig.model_validate({"activity_from": "settings"}).activity_from == S.SETTINGS


def test_the_activity_level_can_be_set_from_the_environment(tmp_path, monkeypatch):
    from harmonist import config

    monkeypatch.setenv("HARMONIST_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HARMONIST_MUSIC_DIR", str(tmp_path / "music"))
    monkeypatch.setenv("HARMONIST_GARDENER_ACTIVITY_FROM", "identity")
    assert config.load().gardener.activity_from == S.IDENTITY


# ---- the Inbox ------------------------------------------------------------


def test_a_structural_update_is_waiting_in_the_inbox_by_default(client, cfg, monkeypatch):
    _flagged_library(cfg, monkeypatch, {"Renumbered": S.STRUCTURE, "Enriched": S.ENRICHMENT})

    section = _updates_section(client.get("/tasks").text)

    assert _headings(section) == ["structure"]
    assert "Renumbered" in section
    assert "Enriched" not in section


def test_lowering_the_threshold_brings_lower_levels_in_grouped_widest_first(
    client, cfg, monkeypatch
):
    _flagged_library(
        cfg,
        monkeypatch,
        {"Renumbered": S.STRUCTURE, "Enriched": S.ENRICHMENT, "Respelled": S.SETTINGS},
    )
    client.app.state.cfg = cfg.model_copy(
        update={"gardener": GardenerConfig(review_from=S.ENRICHMENT)}
    )

    section = _updates_section(client.get("/tasks").text)

    assert _headings(section) == ["structure", "enrichment"]
    assert "Enriched" in section
    assert "Respelled" not in section


def test_an_ignored_update_waits_nowhere(client, cfg, monkeypatch):
    albums = _flagged_library(cfg, monkeypatch, {"Muted": S.STRUCTURE, "Waiting": S.STRUCTURE})
    muted = next(a for a in albums if a.path.name == "Muted")
    activity_store.ignore_update(muted.id, release_version="v1")

    section = _updates_section(client.get("/tasks").text)

    assert "Waiting" in section
    assert "Muted" not in section


def test_an_album_still_in_the_inbox_is_listed_once_under_its_state(client, cfg, monkeypatch):
    _make_tagged_album(cfg, "Settled", mbid="rel-settled", tagged_at=datetime.now(UTC))
    _make_album(cfg, "Unmatched")
    albums = scanner.scan(cfg.paths.music_dir)
    for album in albums:
        album.update_available = True
        album.update_significance = S.STRUCTURE
        album.mb_version = "v1"
    monkeypatch.setattr(scanner, "scan", lambda *args, **kwargs: albums)

    page = client.get("/tasks").text
    section = _updates_section(page)

    assert "Settled" in section
    assert "Unmatched" in page
    assert "Unmatched" not in section


def test_the_inbox_count_includes_waiting_updates(client, cfg, monkeypatch):
    from harmonist import live_counts

    albums = _flagged_library(
        cfg, monkeypatch, {"Renumbered": S.STRUCTURE, "Enriched": S.ENRICHMENT}
    )
    # Module state another test may have left counting albums of its own.
    live_counts.reset_from(albums)

    assert client.get("/status").json()["counts"]["inbox"] == 1


# ---- Settings -------------------------------------------------------------


def test_settings_counts_each_levels_waiting_updates(client, cfg, monkeypatch):
    _flagged_library(
        cfg,
        monkeypatch,
        {"A": S.ENRICHMENT, "B": S.ENRICHMENT, "C": S.STRUCTURE, "D": None},
    )

    page = client.get("/settings").text
    counts = dict(re.findall(r'data-level="(\w+)"[^>]*data-count="(\d+)"', page))

    assert counts == {
        "settings": "0",
        "cosmetic": "0",
        "enrichment": "2",
        "identity": "0",
        "structure": "1",
    }


_FORM = {
    "download_format": "flac",
    "max_downloads_per_sync": "10",
    "user_agent": "Harmonist/test ( test@example.com )",
    "gardener_level": "off",
    "folder_cover": "never",
    "standardize_artist_names": "none",
    "log_level": "info",
}


def test_saving_the_inbox_ticks_keeps_the_narrowest_as_the_level(client, cfg):
    form = {
        **_FORM,
        "gardener_routing": "1",
        "gardener_review": ["structure", "identity", "enrichment"],
    }

    assert client.post("/settings", data=form).status_code == 200

    assert client.app.state.cfg.gardener.review_from == S.ENRICHMENT
    settings_file = (cfg.paths.config_dir / "harmonist.toml").read_text()
    assert re.search(r'review_from\s*=\s*"enrichment"', settings_file)


def test_unticking_every_inbox_level_reviews_nothing(client):
    form = {**_FORM, "gardener_routing": "1"}

    assert client.post("/settings", data=form).status_code == 200

    assert client.app.state.cfg.gardener.review_from == "none"


def test_a_form_without_the_grid_keeps_the_saved_levels(client):
    """Unticked boxes post nothing, so only the grid's own marker can say the
    form had one; without it, absence is not a choice."""
    client.app.state.cfg = client.app.state.cfg.model_copy(
        update={"gardener": GardenerConfig(review_from=S.IDENTITY)}
    )

    assert client.post("/settings", data=_FORM).status_code == 200

    assert client.app.state.cfg.gardener.review_from == S.IDENTITY


@pytest.mark.parametrize(
    ("review_from", "apply_up_to", "expected"),
    [
        (S.STRUCTURE, "none", {"structure": "review"}),
        (
            S.IDENTITY,
            S.COSMETIC,
            {"structure": "review", "identity": "review", "cosmetic": "apply", "settings": "apply"},
        ),
        (
            "none",
            S.IDENTITY,
            {"identity": "apply", "enrichment": "apply", "cosmetic": "apply", "settings": "apply"},
        ),
        # Overlap: applying wins, so the row shows one tick.
        (
            S.SETTINGS,
            S.ENRICHMENT,
            {
                "structure": "review",
                "identity": "review",
                "enrichment": "apply",
                "cosmetic": "apply",
                "settings": "apply",
            },
        ),
    ],
)
def test_the_grid_ticks_each_level_by_where_its_updates_go(
    client, review_from, apply_up_to, expected
):
    client.app.state.cfg = client.app.state.cfg.model_copy(
        update={"gardener": GardenerConfig(review_from=review_from, apply_up_to=apply_up_to)}
    )

    page = client.get("/settings").text
    ticked: dict[str, str] = {}
    for name, value in re.findall(
        r'<input[^>]*name="gardener_(apply|review)"[^>]*value="(\w+)"[^>]*\bchecked\b', page
    ):
        assert value not in ticked, f"{value} ticked in both columns"
        ticked[value] = name

    assert ticked == expected


# ---- the update check's digest -------------------------------------------


def _digest_of(cfg, levels: dict[str, owned.Significance]) -> dict[str, Album]:
    """Record the digest of a pass that newly flagged an album per name, each
    at its level, and return the albums by name."""
    for i, name in enumerate(levels):
        _make_tagged_album(cfg, name, mbid=f"rel-{i}", tagged_at=datetime.now(UTC))
    albums = {a.path.name: a for a in scanner.scan(cfg.paths.music_dir)}
    for name, level in levels.items():
        albums[name].update_significance = level
    activity_store.clear()
    gardener._record_digest(
        gardener.PassResult(
            asked=len(albums),
            examined=len(albums),
            flagged=len(albums),
            newly_flagged=tuple(albums.values()),
            gone=0,
            failed=0,
        )
    )
    return albums


def test_the_digest_records_an_update_left_in_the_library(cfg):
    """#1024. The Inbox takes Structure alone by default, and the enrichment
    stays in the Library — where nothing else records that it arrived, since
    the flag is derived and never stored."""
    albums = _digest_of(cfg, {"Renumbered": S.STRUCTURE, "Enriched": S.ENRICHMENT})

    labels = [e.album_label for e in activity.recent(10)]
    assert albums["Renumbered"].label in labels
    assert albums["Enriched"].label in labels


def test_the_digest_records_from_the_activity_level_whatever_the_inbox_takes(cfg):
    """#1024. The two thresholds answer different questions: an enrichment can
    wait in the Inbox without being recorded, and a structure change be
    recorded with an Inbox that takes nothing."""
    gardener.configure_routing(
        lambda: GardenerConfig(review_from=S.SETTINGS, activity_from=S.IDENTITY)
    )
    albums = _digest_of(cfg, {"Renumbered": S.STRUCTURE, "Enriched": S.ENRICHMENT})

    labels = [e.album_label for e in activity.recent(10)]
    assert albums["Renumbered"].label in labels
    assert albums["Enriched"].label not in labels


def test_the_digest_records_nothing_when_activity_takes_nothing(cfg):
    gardener.configure_routing(lambda: GardenerConfig(activity_from="none"))
    albums = _digest_of(cfg, {"Renumbered": S.STRUCTURE})

    assert all(e.album_label != albums["Renumbered"].label for e in activity.recent(10))


def test_the_digest_records_a_settings_update_only_once_settings_is_chosen(cfg):
    """#1024. Off by default, like any level below the threshold."""
    albums = _digest_of(cfg, {"Spelled": S.SETTINGS})
    assert all(e.album_label != albums["Spelled"].label for e in activity.recent(10))

    gardener.configure_routing(lambda: GardenerConfig(activity_from=S.SETTINGS))
    gardener._record_digest(
        gardener.PassResult(
            asked=1, examined=1, flagged=1, newly_flagged=(albums["Spelled"],), gone=0, failed=0
        )
    )
    assert [e.message for e in activity.recent(10)] == [
        "Update available from MusicBrainz (settings)"
    ]


# ---- the Settings grid's Log column (#1024) -------------------------------


def _log_ticks(page: str) -> set[str]:
    """The levels whose Log box is ticked, read off each box's own tag with
    attribute values blanked, so `checked` can only match the attribute."""
    boxes = re.findall(r'<input[^>]*name="gardener_log"[^>]*>', page)
    return {
        re.search(r'value="(\w+)"', box).group(1)
        for box in boxes
        if "checked" in re.sub(r'"[^"]*"', '""', box)
    }


@pytest.mark.parametrize(
    ("activity_from", "ticked"),
    [
        (S.SETTINGS, {"settings", "cosmetic", "enrichment", "identity", "structure"}),
        (S.COSMETIC, {"cosmetic", "enrichment", "identity", "structure"}),
        (S.IDENTITY, {"identity", "structure"}),
        ("none", set()),
    ],
)
def test_the_log_column_ticks_the_chosen_level_and_above(client, activity_from, ticked):
    client.app.state.cfg = client.app.state.cfg.model_copy(
        update={"gardener": GardenerConfig(activity_from=activity_from)}
    )

    assert _log_ticks(client.get("/settings").text) == ticked


def test_saving_the_log_ticks_keeps_the_narrowest_as_the_level(client, cfg):
    form = {**_FORM, "gardener_routing": "1", "gardener_log": ["structure", "identity"]}

    assert client.post("/settings", data=form).status_code == 200

    assert client.app.state.cfg.gardener.activity_from == S.IDENTITY
    settings_file = (cfg.paths.config_dir / "harmonist.toml").read_text()
    assert re.search(r'^activity_from\s*=\s*"identity"', settings_file, re.MULTILINE)


def test_a_form_without_the_grid_keeps_the_saved_activity_level(client):
    client.app.state.cfg = client.app.state.cfg.model_copy(
        update={"gardener": GardenerConfig(activity_from=S.STRUCTURE)}
    )

    assert client.post("/settings", data=_FORM).status_code == 200

    assert client.app.state.cfg.gardener.activity_from == S.STRUCTURE
