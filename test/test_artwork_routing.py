"""Where an album's artwork update waits, by what it does (#852).

The user picks the least an artwork update must do for the Inbox to take it:
every addition and replacement, replacements only, or nothing. An album is
judged by its plan as a whole, so one that replaces anything is a replacement.
An album with a "Better artwork wanted" watch waits in the Inbox whatever the
threshold says. The rest stay findable through the Library's Artwork updates
filter, which is where every artwork update waited before there was a choice.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from harmonist import (
    activity,
    activity_store,
    artwork,
    artwork_updates,
    caa_cache,
    cover_art,
    formats,
    images,
    scanner,
)
from harmonist.config import GardenerConfig
from harmonist.models import Album, AlbumState, ArtworkWatch
from test import test_web
from test.test_artwork import png_bytes
from test.test_gardener import _release, _tagged
from test.test_update_routing import _FORM

cfg = test_web.cfg
client = test_web.client

A = artwork.Operation
MBID = "rel-art"
FRONT = "https://example.test/front.png"


def _archive(data: bytes, *, mbid: str = MBID) -> activity_store.CachedCoverArt:
    """The archive's front for `mbid`, as a background check leaves it."""
    image = formats.EmbeddedArt.of(data, "image/png")
    answer = activity_store.CachedCoverArt(
        fetched_at=datetime.now(UTC),
        image_url=FRONT,
        width=image.size.width if image.size else None,
        height=image.size.height if image.size else None,
        mime="image/png",
        source="release",
    )
    cover_art.cache_image(mbid, data, "image/png")
    activity_store.store_cover_art(mbid, answer)
    return answer


def _album(root: Path, name: str, *, cover: bytes | None = None, mbid: str = MBID) -> Album:
    """A tagged album with no embedded art, and `cover` as its folder cover."""
    album = _tagged(root, _release(mbid=mbid), name=name)
    if cover is not None:
        (album.path / "cover.png").write_bytes(cover)
    return next(a for a in scanner.scan(root) if a.path == album.path)


def _routing(*, review="none", apply="none") -> GardenerConfig:
    return GardenerConfig(artwork_review_from=review, artwork_apply_up_to=apply)


def _route(client, review_from, apply_up_to="none") -> None:
    """Set the thresholds on the running app, as a Settings save does."""
    state = client.app.state
    state.cfg = state.cfg.model_copy(
        update={"gardener": _routing(review=review_from, apply=apply_up_to)}
    )


def _section(html: str) -> str:
    match = re.search(r'<section[^>]*id="inbox-artwork".*?</section>', html, re.DOTALL)
    return match.group(0) if match else ""


def _headings(section: str) -> list[str]:
    return re.findall(r'id="inbox-artwork-(\w+)"', section)


def _card(section: str, album: Album) -> str:
    """One album's card, up to the next card or the end of its group."""
    match = re.search(
        rf'<div id="task-artwork-{re.escape(album.id)}".*?(?=<div id="task-artwork-|</section>)',
        section,
        re.DOTALL,
    )
    return match.group(0) if match else ""


@pytest.fixture
def library(client, cfg):
    """One album the archive's larger front would add artwork to, and one whose
    smaller folder cover it would replace, both warmed as at startup."""
    big = png_bytes(600, 600)
    _archive(big)
    root = cfg.paths.music_dir
    _album(root, "Bare")
    _album(root, "Small Cover", cover=png_bytes(100, 100))
    albums = {a.path.name: a for a in scanner.scan(root)}
    artwork_updates.warm(list(albums.values()))
    assert artwork_updates.operation(albums["Bare"]) is A.ADDITION
    assert artwork_updates.operation(albums["Small Cover"]) is A.REPLACEMENT
    return albums, images.digest(big)


# ---- the threshold itself -------------------------------------------------


@pytest.mark.parametrize(
    ("threshold", "routed"),
    [(A.ADDITION, {A.ADDITION, A.REPLACEMENT}), (A.REPLACEMENT, {A.REPLACEMENT}), ("none", set())],
)
def test_the_inbox_takes_the_chosen_operation_and_every_one_after_it(threshold, routed):
    assert {op for op in artwork_updates.ORDER if artwork_updates.reviews(op, threshold)} == routed


def test_artwork_updates_stay_in_the_library_unless_told_otherwise():
    assert GardenerConfig().artwork_review_from == "none"


def test_an_unknown_artwork_threshold_is_refused_with_the_operations_named():
    with pytest.raises(ValueError, match="addition"):
        GardenerConfig.model_validate({"artwork_review_from": "everything"})


def test_the_artwork_threshold_can_be_set_from_the_environment(tmp_path, monkeypatch):
    from harmonist import config

    monkeypatch.setenv("HARMONIST_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HARMONIST_MUSIC_DIR", str(tmp_path / "music"))
    monkeypatch.setenv("HARMONIST_GARDENER_ARTWORK_REVIEW_FROM", "replacement")
    assert config.load().gardener.artwork_review_from == A.REPLACEMENT


# ---- which albums ---------------------------------------------------------


def test_no_threshold_and_no_watch_builds_no_plan(library, monkeypatch):
    albums, _ = library

    def built(*args, **kwargs):
        pytest.fail("a plan was built for an album nothing could route")

    monkeypatch.setattr(artwork_updates, "_plan", built)
    assert artwork_updates.for_review(list(albums.values()), GardenerConfig()) == []


def test_a_watched_album_waits_in_the_inbox_whatever_the_threshold(library):
    albums, _ = library
    bare = albums["Bare"]
    assert bare.sidecar is not None
    watched = replace(bare, sidecar=replace(bare.sidecar, artwork_watch=ArtworkWatch(MBID)))

    assert artwork_updates.for_review([bare, watched], GardenerConfig()) == [watched]
    assert artwork_updates.for_review([watched], _routing(review=A.REPLACEMENT)) == [watched]
    # Nor does an apply range write it: the watch asked to be shown.
    assert artwork_updates.for_review([watched], _routing(apply=A.ADDITION)) == [watched]


def test_an_album_still_in_an_inbox_state_is_listed_there_not_routed(library):
    albums, _ = library
    bare = albums["Bare"]
    assert bare.state is AlbumState.COMPLETE
    unmatched = replace(bare, state=AlbumState.NEEDS_MBID)

    assert artwork_updates.for_review([bare, unmatched], _routing(review=A.ADDITION)) == [bare]
    assert artwork_updates.waiting_by_operation([bare, unmatched])[A.ADDITION] == 1


# ---- the Inbox ------------------------------------------------------------


@pytest.mark.parametrize(
    ("threshold", "headings"),
    [("none", []), (A.REPLACEMENT, ["replacement"]), (A.ADDITION, ["replacement", "addition"])],
)
def test_the_inbox_groups_what_the_threshold_takes_replacements_first(
    client, library, threshold, headings
):
    _route(client, threshold)

    assert _headings(_section(client.get("/tasks").text)) == headings


def _sides(card: str) -> list[tuple[str | None, str]]:
    """Each side of a card's pair: the image it shows, if any, and its caption
    with the line break as a space."""
    sides = re.findall(r'<figure class="art-card__side">(.*?)</figure>', card, re.DOTALL)
    out = []
    for side in sides:
        src = re.search(r'<img src="([^"]+)"', side)
        caption = re.search(r"<figcaption[^>]*>(.*?)</figcaption>", side, re.DOTALL)
        assert caption is not None
        out.append((src.group(1) if src else None, caption.group(1).replace("<br>", " ").strip()))
    return out


def test_an_artwork_cards_title_is_an_in_app_link(client, library):
    """#935: the title opens Harmonist's album page, so it is drawn as one."""
    albums, _digest = library
    _route(client, A.ADDITION)

    card = _card(_section(client.get("/tasks").text), albums["Bare"])
    link = re.search(rf'<a href="/album/{re.escape(albums["Bare"].id)}"[^>]*>', card)

    assert link
    assert "app-link" in re.search(r'class="([^"]*)"', link.group(0)).group(1).split()


def test_an_artwork_card_shows_the_change_as_two_images_with_their_facts(client, library):
    albums, digest = library
    _route(client, A.ADDITION)

    section = _section(client.get("/tasks").text)
    bare = _card(section, albums["Bare"])
    small = _card(section, albums["Small Cover"])

    # An addition: nothing there now, beside what would be written.
    assert _sides(bare) == [
        (None, "No artwork"),
        (f"/artwork/image/{albums['Bare'].id}/{digest}", "600×600 PNG · 2 KB"),
    ]
    assert ">Updates 1 track</p>" in bare
    # Additions and a replacement: the card shows the replacement, the folder
    # cover it would overwrite beside the archive's front.
    small_cover = images.digest((albums["Small Cover"].path / "cover.png").read_bytes())
    assert _sides(small) == [
        (f"/artwork/image/{albums['Small Cover'].id}/{small_cover}", "100×100 PNG · 0 KB"),
        (f"/artwork/image/{albums['Small Cover'].id}/{digest}", "600×600 PNG · 2 KB"),
    ]
    assert ">Updates 1 track and cover.png</p>" in small


def test_a_card_counts_the_tracks_it_writes_against_the_whole_album(client, cfg):
    image = png_bytes(600, 600)
    _archive(image)
    album = _tagged(cfg.paths.music_dir, _release(mbid=MBID, tracks=3), name="Gaps", tracks=3)
    formats.write_cover(min(album.path.glob("*.m4a")), image)
    album = next(a for a in scanner.scan(cfg.paths.music_dir) if a.path == album.path)
    artwork_updates.warm([album])

    proposal = artwork_updates.proposal(album)

    assert proposal is not None
    assert proposal.summary == "Updates 2 of 3 tracks"


def test_the_inbox_count_includes_waiting_artwork_updates(client, library):
    _route(client, "none")
    before = client.get("/status").json()["counts"]["inbox"]

    _route(client, A.ADDITION)

    assert client.get("/status").json()["counts"]["inbox"] == before + 2


@pytest.mark.parametrize(
    ("changed", "total", "cover", "line"),
    [
        (12, 12, None, "Updates all 12 tracks"),
        (3, 12, None, "Updates 3 of 12 tracks"),
        (1, 1, None, "Updates 1 track"),
        (0, 12, "cover.jpg", "Updates cover.jpg only"),
        (12, 12, "cover.jpg", "Updates all 12 tracks and cover.jpg"),
        (3, 12, "cover.png", "Updates 3 of 12 tracks and cover.png"),
    ],
)
def test_a_card_says_which_files_its_update_writes_in_one_line(changed, total, cover, line):
    image = formats.EmbeddedArt.of(png_bytes(10, 10), "image/png")
    proposal = artwork_updates.Proposal(
        operation=A.REPLACEMENT,
        before=image,
        after=image,
        tracks_total=total,
        tracks_added=0,
        tracks_replaced=changed,
        cover=A.REPLACEMENT if cover else None,
        cover_name=cover,
        fingerprint="f",
    )

    assert proposal.summary == line


@pytest.mark.parametrize(
    ("changed", "line"),
    [
        (0, "Renames cover.jpg to cover.png"),
        (1, "Updates 1 track and renames cover.jpg to cover.png"),
    ],
)
def test_a_card_says_a_cover_is_renamed_rather_than_updated(changed, line):
    """Its image stays as it is, so "Updates cover.jpg" would be wrong (#907)."""
    image = formats.EmbeddedArt.of(png_bytes(10, 10), "image/png")
    proposal = artwork_updates.Proposal(
        operation=A.ADDITION,
        before=image,
        after=image,
        tracks_total=1,
        tracks_added=changed,
        tracks_replaced=0,
        cover=A.ADDITION,
        cover_name="cover.jpg",
        fingerprint="f",
        cover_renamed_to="cover.png",
    )

    assert proposal.summary == line


# ---- Settings -------------------------------------------------------------


def _artwork_rows(html: str) -> dict[str, tuple[int, bool]]:
    """Each artwork row's count, and whether its Inbox box is ticked, read off
    the box's own tag with attribute values blanked, so `checked` can only
    match the attribute itself."""
    rows = re.findall(
        r'<li class="update-routing__row" data-operation="(\w+)" data-count="(\d+)">(.*?)</li>',
        html,
        re.DOTALL,
    )
    out = {}
    for op, n, body in rows:
        box = re.search(r'<input[^>]*name="gardener_artwork_review"[^>]*>', body)
        assert box is not None
        out[op] = (int(n), "checked" in re.sub(r'"[^"]*"', '""', box.group(0)))
    return out


@pytest.mark.parametrize(
    ("threshold", "ticked"),
    [("none", set()), (A.REPLACEMENT, {"replacement"}), (A.ADDITION, {"addition", "replacement"})],
)
def test_settings_counts_and_ticks_each_operation(client, library, threshold, ticked):
    _route(client, threshold)

    rows = _artwork_rows(client.get("/settings").text)

    assert list(rows) == ["replacement", "addition"]
    assert {op: n for op, (n, _) in rows.items()} == {"replacement": 1, "addition": 1}
    assert {op for op, (_, checked) in rows.items() if checked} == ticked


@pytest.mark.parametrize(
    ("ticked", "saved"),
    [(["replacement", "addition"], A.ADDITION), (["replacement"], A.REPLACEMENT), ([], "none")],
)
def test_saving_the_artwork_ticks_keeps_the_least_as_the_threshold(client, ticked, saved):
    form = {**_FORM, "gardener_artwork_routing": "1", "gardener_artwork_review": ticked}

    assert client.post("/settings", data=form).status_code == 200

    assert client.app.state.cfg.gardener.artwork_review_from == saved


def test_a_form_without_the_artwork_grid_keeps_the_saved_threshold(client):
    _route(client, A.REPLACEMENT)

    assert client.post("/settings", data=_FORM).status_code == 200

    assert client.app.state.cfg.gardener.artwork_review_from == A.REPLACEMENT


# ---- the background check's digest ----------------------------------------


@pytest.fixture
def unchecked(client, cfg, monkeypatch):
    """An album with no artwork that the archive has not been asked about yet,
    and an archive that answers with a front."""
    root = cfg.paths.music_dir
    album = _album(root, "Bare")
    artwork_updates.warm([album])
    assert artwork_updates.operation(album) is None
    monkeypatch.setattr(cover_art, "check_front", lambda mbid, **kw: _archive(png_bytes(600, 600)))
    activity_store.clear()
    return album


def _announced(album: Album) -> list[str]:
    return [e.message for e in activity.recent(20) if e.album_id == album.id]


def test_the_check_announces_an_album_it_sends_to_the_inbox_once(unchecked, monkeypatch):
    artwork_updates.configure(
        lambda: artwork.FolderCoverPolicy.NEVER, lambda: _routing(review=A.ADDITION)
    )
    artwork_updates.warm([unchecked])

    assert artwork_updates.sweep([unchecked], limit=5) == 1
    assert _announced(unchecked) == [
        "Artwork update available from the Cover Art Archive (addition)"
    ]

    # Asked again, the archive says the same: still waiting, so not news.
    artwork_updates._attempted.clear()
    monkeypatch.setattr(caa_cache, "_ttl", timedelta(0))
    assert artwork_updates.sweep([unchecked], limit=5) == 1
    assert len(_announced(unchecked)) == 1


def test_the_check_does_not_announce_an_update_left_in_the_library(unchecked):
    artwork_updates.configure(lambda: artwork.FolderCoverPolicy.NEVER, GardenerConfig)
    artwork_updates.warm([unchecked])

    assert artwork_updates.sweep([unchecked], limit=5) == 1

    assert artwork_updates.operation(unchecked) is A.ADDITION
    assert _announced(unchecked) == []
