"""Filling a suggestion's missing disc from another unmatched folder (#876)."""

import shutil
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from harmonist import album_files, disc_fill, formats, scanner, track_assignment
from harmonist import sidecar as sc
from harmonist.models import Album, AlbumState, MatchCandidate, Sidecar
from test import test_web
from test.conftest import SINE_M4A
from test.helpers import finish_writes
from test.test_web import _confirmation_fields, _id_for, _release_for_match

cfg = test_web.cfg
client = test_web.client

MBID = "rel-split"


def _two_discs(lengths: tuple[int, int] = (1000, 1000)) -> dict:
    """Two discs of two tracks; the second disc's lengths are `lengths`."""
    release = _release_for_match(MBID, n_tracks=4)
    tracks = release["medium-list"][0]["track-list"]
    for track, length in zip(tracks[2:], lengths, strict=True):
        track["recording"]["length"] = str(length)
    release["medium-list"] = [
        {
            "position": str(disc),
            "track-list": [{**t, "position": str(i)} for i, t in enumerate(part, 1)],
        }
        for disc, part in ((1, tracks[:2]), (2, tracks[2:]))
    ]
    return release


def _folder(root: Path, n: int = 2) -> Path:
    """`n` untagged one-second files numbered from 1, as a ripper names a disc."""
    root.mkdir(parents=True)
    for i in range(1, n + 1):
        shutil.copy(SINE_M4A, root / f"{i:02d} Track.m4a")
        formats.write_tags(
            root / f"{i:02d} Track.m4a",
            formats.TagSet(
                mb_album_id="",
                album="Split",
                album_artist="VA",
                title="T",
                artist="VA",
                track_num=i,
                track_total=n,
            ),
            None,
        )
    return root


def _album(album_id: str, path: str = "x", count: int = 2) -> Album:
    return Album(
        id=album_id, path=Path(path), title="", artist="", track_count=count, state=AlbumState.NEW
    )


# ---------- the pairing ----------


def test_an_added_folder_fills_its_disc_whatever_order_the_folders_sort_in(tmp_path):
    # The added folder sorts first and its files say tracks 1 and 2, so file
    # order and track numbers both point it at disc 1. The choice of disc wins.
    added = album_files.audio_files(_folder(tmp_path / "A second half"))
    primary = album_files.audio_files(_folder(tmp_path / "B first half"))
    files = [*added, *primary]
    panel = track_assignment.panel(files, _two_discs(), fill={2: added})
    assert panel.mapping() == {added[0]: 2, added[1]: 3, primary[0]: 0, primary[1]: 1}


# ---------- which folder fits ----------


def test_a_missing_disc_is_one_with_no_file_paired_to_any_of_its_tracks(tmp_path):
    release = _two_discs((1000, 9000))
    panel = track_assignment.panel(album_files.audio_files(_folder(tmp_path / "d1")), release)
    assert disc_fill.gaps(panel, release) == [disc_fill.Gap(2, (1000, 9000))]


@pytest.mark.parametrize(
    ("lengths", "fits"),
    [
        ((1000, 9000), True),
        ((5000, 9000), True),  # 4s out: the match tolerance
        ((5001, 9000), False),
        ((None, 9000), False),  # a file with no length can't vote
        ((1000,), False),
        ((1000, 9000, 1000), False),
    ],
)
def test_a_folder_fits_only_track_for_track_within_the_tolerance(lengths, fits):
    assert disc_fill.fits(disc_fill.Gap(2, (1000, 9000)), lengths) is fits


def test_a_folder_is_filled_in_only_when_it_is_the_one_that_fits():
    one, two = _album("one"), _album("two")
    assert disc_fill.automatic({2: [disc_fill.Candidate(one, True)]}) == {2: one}
    # Two that fit are the user's choice, and so is one that doesn't fit.
    assert not disc_fill.automatic(
        {2: [disc_fill.Candidate(one, True), disc_fill.Candidate(two, True)]}
    )
    assert not disc_fill.automatic({2: [disc_fill.Candidate(one, False)]})
    # One folder can't be two discs.
    assert not disc_fill.automatic(
        {2: [disc_fill.Candidate(one, True)], 3: [disc_fill.Candidate(one, True)]}
    )


# ---------- the review ----------


@pytest.fixture
def split(cfg, monkeypatch):
    """`Disc 1` with the release suggested, `Disc 2` adopted as an album of its own."""
    release = _two_discs()
    root = cfg.paths.music_dir / "VA" / "Split"
    first, second = _folder(root / "Disc 1"), _folder(root / "Disc 2")
    sc.write(
        first,
        Sidecar(
            mb_match_candidate=MatchCandidate(
                mb_release_id=MBID, confidence="approximate", file_count=2, track_count=4
            )
        ),
    )
    monkeypatch.setattr("harmonist.mb_lookup.fetch_release", lambda mbid: release)
    return first, second


def _review(client, aid: str) -> str:
    """The suggestion as the Inbox card shows it, once the release is cached."""
    client.get(f"/assignments/{aid}")
    return client.get(f"/assignments/{aid}?cancel=true").text


def _disc_rows(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    return {
        str(row["data-disc-fill"]): row.get_text(" ", strip=True)
        for row in soup.select("[data-disc-fill]")
    }


def test_the_one_folder_that_fits_is_in_the_review_from_the_start(client, cfg, split):
    first, second = split
    _folder(cfg.paths.music_dir / "Other" / "Three", n=3)  # the wrong size
    review = _review(client, _id_for(cfg, first))
    assert _disc_rows(review) == {"2": "Disc 2 from VA/Split/Disc 2 Remove"}
    assert _confirmation_fields(review)["fill"] == f"2:{_id_for(cfg, second)}"
    assert _confirmation_fields(review)["incomplete"] == "false"


def test_two_folders_that_fit_are_offered_and_neither_is_chosen(client, cfg, split):
    first, second = split
    other = _folder(cfg.paths.music_dir / "Other" / "Two")
    review = _review(client, _id_for(cfg, first))
    soup = BeautifulSoup(review, "html.parser")
    assert "fill" not in _confirmation_fields(review)
    options = [o["value"] for o in soup.select("select[name=fill_add] option") if o["value"]]
    assert options == [f"2:{_id_for(cfg, other)}", f"2:{_id_for(cfg, second)}"]


def test_a_removed_folder_stays_out_of_the_review(client, cfg, split):
    first, second = split
    aid = _id_for(cfg, first)
    fields = _confirmation_fields(_review(client, aid))
    removed = client.post(f"/assignments/{aid}", data=fields | {"fill_remove": "2"})
    assert "fill" not in _confirmation_fields(removed.text)
    # …and is offered again, among the folders that fit.
    fitting = BeautifulSoup(removed.text, "html.parser").select(
        "select[name=fill_add] optgroup[label='Track lengths match'] option"
    )
    assert [o["value"] for o in fitting] == [f"2:{_id_for(cfg, second)}"]
    # Further changes to the review don't bring it back.
    reset = client.post(
        f"/assignments/{aid}",
        data=_confirmation_fields(removed.text) | {"assignment_action": "reset"},
    )
    assert "fill" not in _confirmation_fields(reset.text)


def test_confirming_tags_both_folders_and_they_become_one_album(client, cfg, split):
    first, second = split
    aid = _id_for(cfg, first)
    fields = _confirmation_fields(_review(client, aid))
    applied = client.post(f"/confirm/{aid}/accept", data=fields)
    assert "confirmation-applied" in applied.headers.get("HX-Trigger", "")
    finish_writes(client.app)
    for folder, disc in ((first, 1), (second, 2)):
        assert sc.read(folder).mb_release_id == MBID
        assert {formats.read_tags(f).disc_num for f in album_files.audio_files(folder)} == {disc}
    (album,) = [a for a in scanner.scan(cfg.paths.music_dir) if first in a.folders]
    assert sorted(album.folders) == [first, second]
    assert album.state == AlbumState.COMPLETE


def test_the_artwork_confirmed_with_the_review_reaches_the_added_folder_too(client, cfg, split):
    """The artwork plan the review shows is the plan for every folder it tags.

    Drawn over the suggested folder alone, it was a different plan from the
    one the tagging rebuilt over both, so a confirmation with artwork tagged
    the album and wrote none of it.
    """
    from datetime import UTC, datetime

    from harmonist import activity_store, cover_art, images

    first, second = split
    aid = _id_for(cfg, first)
    image = test_web._png(61)
    cover_art.configure_cache(cfg.paths.config_dir / "caa")
    cover_art.cache_image(MBID, image, "image/png")
    activity_store.store_cover_art(
        MBID,
        activity_store.CachedCoverArt(
            fetched_at=datetime.now(UTC), image_url="https://example.com/cover.png"
        ),
    )
    review = _review(client, aid)
    # The artwork the review loads, from the address the review gives it.
    load = BeautifulSoup(review, "html.parser").select_one(".assignment-artwork [hx-get]")
    assert load is not None
    artwork = client.get(str(load["hx-get"]))
    assert "Use artwork from the selected release" in artwork.text
    fields = _confirmation_fields(review) | _confirmation_fields(artwork.text)
    applied = client.post(f"/confirm/{aid}/accept", data=fields | {"include_artwork": "true"})
    assert "confirmation-applied" in applied.headers.get("HX-Trigger", "")
    finish_writes(client.app)
    for folder in (first, second):
        arts = {formats.read_tags(f).art for f in album_files.audio_files(folder)}
        assert {a.digest if a else None for a in arts} == {images.digest(image)}, folder


def test_a_folder_matched_since_the_review_is_not_taken(client, cfg, split):
    first, second = split
    aid = _id_for(cfg, first)
    fields = _confirmation_fields(_review(client, aid))
    sc.write(second, Sidecar(mb_release_id="rel-elsewhere"))
    applied = client.post(f"/confirm/{aid}/accept", data=fields)
    finish_writes(client.app)
    assert "has changed" in applied.text
    assert sc.read(second).mb_release_id == "rel-elsewhere"
    assert sc.read(first).mb_release_id is None


# ---------- the Inbox ----------


def _inbox_cards(client) -> set[str]:
    soup = BeautifulSoup(client.get("/tasks").text, "html.parser")
    return {str(card["id"]).removeprefix("task-") for card in soup.select('div[id^="task-"]')}


def test_the_folder_a_review_fills_is_not_also_an_album_in_the_inbox(client, cfg, split):
    first, second = split
    _review(client, _id_for(cfg, first))  # the release, cached as a suggestion's is
    cards = _inbox_cards(client)
    assert _id_for(cfg, first) in cards
    assert _id_for(cfg, second) not in cards


def test_the_inbox_badge_counts_what_the_inbox_lists(client, cfg, split):
    from harmonist import live_counts

    first, _ = split
    _review(client, _id_for(cfg, first))
    live_counts.reset_from(scanner.scan(cfg.paths.music_dir))  # as a scan publishes them
    counts = client.get("/status").json()["counts"]
    # Disc 1, waiting on its suggestion; Disc 2 is a disc of it, not a New album.
    assert (counts["inbox"], counts["needs_mbid"], counts["new"]) == (1, 1, 0)


def test_a_folder_the_review_only_offers_stays_in_the_inbox(client, cfg, split):
    first, second = split
    other = _folder(cfg.paths.music_dir / "Other" / "Two")
    _review(client, _id_for(cfg, first))
    cards = _inbox_cards(client)
    assert {_id_for(cfg, first), _id_for(cfg, second), _id_for(cfg, other)} <= cards
