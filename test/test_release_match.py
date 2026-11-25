"""Release-match review before MB contributions (#618)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

import musicbrainzngs
import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from mutagen.mp4 import MP4

from harmonist import activity_store, contributions, gardener, mb_cache, mb_lookup, scanner, sidecar
from harmonist.config import Config, PathsConfig
from harmonist.models import MismatchDismissal, Release, Sidecar
from harmonist.web.main import _library_page_vars, create_app
from test.test_contributions import (
    MBID,
    NOW,
    URL,
    album,
    before_dismissal,
    harmony_link,
    make_files,
    release,
)

# The White Clouds case: the download's store renamed its page, and the matched
# release links a label's store instead.
RENAMED = "https://artist.bandcamp.com/album/artist-record"
LABEL = "https://label.bandcamp.com/album/record"


@pytest.fixture(autouse=True)
def store(tmp_path):
    activity_store.init(tmp_path / "activity.db")


def observed(a, current_urls=(LABEL,), siblings=(), formats=("Digital Media",)):
    current = release(formats, current_urls)
    a = contributions.observe(a, current, NOW)
    a = contributions.observe_group(a, [current, *siblings], 1 + len(siblings))
    return contributions.assess(a)


@pytest.mark.parametrize(
    ("current_urls", "sibling_urls", "reasons", "finding"),
    [
        # Another release links the download's store; the match links none of it.
        ((LABEL,), (RENAMED,), ("same_store",), "missing_url"),
        # The match links a page on that store too: cross-listing, not evidence.
        ((LABEL, "https://artist.bandcamp.com/album/other"), (RENAMED,), (), "same_store"),
        # Another store's page is no evidence either way.
        ((LABEL,), ("https://someone.bandcamp.com/album/record",), (), "missing_url"),
        # The exact URL on another digital release.
        ((LABEL,), (URL,), ("source_match",), None),
        # ...which is no evidence against a match that links it too.
        ((URL,), (URL,), (), None),
    ],
)
def test_mismatch_reasons_and_the_contribution_behind_them(
    current_urls, sibling_urls, reasons, finding
):
    sibling = release(urls=sibling_urls, mbid="sibling")
    assessment = observed(album(), current_urls, [sibling])
    shown = contributions.panel(assessment, assessment.siblings)
    assert shown is not None
    assert shown.reasons == reasons
    # Accepting a match does not make another release's URL a contribution.
    assert shown.finding == finding


@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("sibling_format", ["Digital Media", "CD"])
@pytest.mark.parametrize("source_upc", [None, "0801061000332"])
def test_store_url_on_another_release_is_not_a_contribution(accepted, sibling_format, source_upc):
    a = album()
    a.track_count = 28
    a.source_upc = source_upc
    if accepted:
        a.sidecar = replace(a.sidecar, accepted_release_id=MBID)
    current = release()
    current["medium-list"][0]["track-count"] = 28
    sibling = release((sibling_format,), urls=(URL,), mbid="shorter-release")
    sibling["medium-list"][0]["track-count"] = 25
    a = contributions.observe(a, current, NOW)
    a = contributions.observe_group(a, [current, sibling], 2)
    assessment = contributions.assess(a)
    assert assessment.siblings is not None
    shown = contributions.panel(assessment, assessment.siblings)
    assert shown is not None
    assert shown.finding == ("barcode_missing" if source_upc else None)
    mismatch = sibling_format == "Digital Media" and not accepted
    assert contributions.possible_mismatch(assessment) is mismatch
    assert contributions.contribution_due(assessment) is (bool(source_upc) and not mismatch)
    assert a.sidecar.mb_release_id == MBID and a.track_count == 28


def test_an_exact_store_url_match_is_the_only_reason_it_needs():
    """#652: another release linking the download's exact URL settles which
    release it is. A third linking another page on that store is weaker
    evidence, and stating it too only muddied the answer — it keeps its row in
    the list, but adds no reason."""
    exact = release(urls=(URL,), mbid="exact")
    other_page = release(urls=(RENAMED,), mbid="other-page")
    assessment = observed(album(), (LABEL,), [exact, other_page])
    shown = contributions.panel(assessment, assessment.siblings)
    assert shown is not None
    assert shown.reasons == ("source_match",)
    assert [r["id"] for r in assessment.siblings.same_store] == ["other-page"]


@pytest.mark.parametrize(("current", "reasons"), [("0801061000332", ()), (None, ("source_match",))])
def test_a_reused_barcode_is_evidence_only_where_the_match_lacks_it(current, reasons):
    a = album()
    a.source_upc = "0801061000332"
    matched = release(urls=(URL,))
    matched["barcode"] = current
    sibling = release(mbid="sibling")
    sibling["barcode"] = "801061000332"  # the same GTIN, spelt without its zero
    a = contributions.observe(a, matched, NOW)
    a = contributions.observe_group(a, [matched, sibling], 2)
    assessment = contributions.assess(a)
    assert assessment.siblings is not None
    shown = contributions.panel(assessment, assessment.siblings)
    assert shown is not None and shown.reasons == reasons
    assert contributions.possible_mismatch(assessment) is bool(reasons)


def test_an_incomplete_group_places_no_album_even_one_matched_to_a_cd():
    unknown = release(("",), mbid="unknown-media")
    for accepted in (False, True):
        a = album()
        if accepted:
            a.sidecar = replace(a.sidecar, accepted_release_id=MBID)
        current = release(("CD",))
        a = contributions.observe(a, current, NOW)
        a = contributions.observe_group(a, [current, unknown], 2)
        assessment = contributions.assess(a)
        assert assessment.siblings is not None and not assessment.siblings.complete
        assert not contributions.possible_mismatch(assessment)
        assert not contributions.contribution_due(assessment)


def test_sibling_classification_is_reused_until_the_browse_changes():
    a = album()
    current = release(urls=(LABEL,))
    a = contributions.observe(a, current, NOW)
    a = contributions.observe_group(a, [current, release(urls=(RENAMED,), mbid="sibling")], 2)
    first = contributions.assess(a).siblings
    assert first is not None and contributions.assess(a).siblings is first
    a = contributions.observe_group(a, [current], 1)
    again = contributions.assess(a).siblings
    assert again is not first and again is not None and again.same_store == []


@pytest.mark.parametrize(
    ("accepted", "requested", "tagged", "accept", "expected"),
    [
        (None, "b", "b", True, "b"),  # a release chosen and confirmed
        ("a", "a", "a", False, "a"),  # a re-tag of the accepted release
        ("a", "a", "a-merged", False, "a-merged"),  # a merge renames it
        ("a", "b", "b", False, None),  # a rematch clears it...
        (None, "a", "a", False, None),  # ...so matching back does not revive it
    ],
)
def test_acceptance_after_tagging(accepted, requested, tagged, accept, expected):
    from harmonist.retag import _accepted_after_tagging

    assert _accepted_after_tagging(accepted, requested, tagged, accept=accept) == expected


def test_failed_group_browse_keeps_the_stored_one(monkeypatch):
    a = album()
    current = release(urls=(LABEL,))
    current["release-group"] = {"id": "rg-kept"}
    stored = {"release-list": [release(urls=(RENAMED,), mbid="sibling")], "release-count": 1}
    activity_store.store_release("rg-kept", mb_cache._group_key(), stored)
    monkeypatch.setattr(mb_lookup, "fetch_release", lambda mbid: current)
    monkeypatch.setattr(gardener, "refresh_flag", lambda a, r: gardener.Assessment(None, None, a))
    monkeypatch.setattr(
        musicbrainzngs, "browse_releases", Mock(side_effect=musicbrainzngs.NetworkError("down"))
    )
    result = gardener.sweep([a], recheck_after=timedelta(0), limit=1)
    a = result.assessments[0].album
    assert mb_cache.stored_release_group_editions("rg-kept") == (stored["release-list"], 1)
    assert contributions.possible_mismatch(contributions.assess(a))


def test_accepted_physical_release_offers_its_link_but_not_the_download_barcode():
    a = album()
    a.source_upc = "0801061000332"
    a.sidecar = replace(a.sidecar, accepted_release_id=MBID)
    current = release(("CD",))
    current["barcode"] = "0801061000639"
    a = contributions.observe(a, current, NOW)
    a = contributions.observe_group(a, [current], 1)
    assessment = contributions.assess(a)
    assert assessment.siblings is not None
    shown = contributions.panel(assessment, assessment.siblings)
    assert shown is not None and shown.reasons == ("media",)
    assert shown.finding == "missing_url"
    assert not contributions.possible_mismatch(assessment)
    assert contributions.contribution_due(assessment)


def test_filters_split_on_acceptance_and_need_the_group_to_clear_an_album():
    albums = []
    for name, accepted, browsed, sibling_urls in (
        ("Flagged", False, True, (RENAMED,)),
        ("Accepted", True, True, (RENAMED,)),
        ("Clean group", False, True, ()),
        ("Unbrowsed", False, False, ()),
        ("Accepted, unbrowsed", True, False, ()),
    ):
        a = album()
        a.id = a.title = name
        if accepted:
            a.sidecar = replace(a.sidecar, accepted_release_id=MBID)
        current = release(urls=(LABEL,))
        a = contributions.observe(a, current, NOW)
        if browsed:
            sibling = release(urls=sibling_urls, mbid="sibling")
            a = contributions.observe_group(a, [current, sibling], 2)
        albums.append(a)
    mismatch = _library_page_vars(albums, 1, 30, filter_="possible-mismatch")["rows"]
    due = _library_page_vars(albums, 1, 30, filter_="mb-contributions")["rows"]
    assert [a.title for a in mismatch] == ["Flagged"]
    # Nothing is placed before the group is compared, accepted or not.
    assert {a.title for a in due} == {"Accepted", "Clean group"}


def test_accepted_release_round_trips_and_is_audited(tmp_path):
    folder = tmp_path / "album"
    folder.mkdir()
    sidecar.write(folder, Sidecar(mb_release_id=MBID))
    assert "accepted_release_id" not in json.loads((folder / ".harmonist.json").read_text())
    written = sidecar.read(folder)
    assert written is not None and written.accepted_release_id is None
    sidecar.write(folder, replace(written, accepted_release_id=MBID))
    assert json.loads((folder / ".harmonist.json").read_text())["accepted_release_id"] == MBID
    assert sidecar.read(folder).accepted_release_id == MBID
    assert any(f"accepted_release_id=None->{MBID}" in e.message for e in activity_store.recent(20))


def test_merged_parts_keep_acceptance_of_this_release_only():
    accepted = Sidecar(mb_release_id=MBID, accepted_release_id=MBID)
    stale = Sidecar(mb_release_id=MBID, accepted_release_id="older-release")
    assert scanner._merge_sidecars(
        [Sidecar(mb_release_id=MBID), accepted], MBID
    ).accepted_release_id
    assert scanner._merge_sidecars([stale], MBID).accepted_release_id is None


def _app(tmp_path, monkeypatch, current):
    cfg = Config(paths=PathsConfig(music_dir=tmp_path / "music", config_dir=tmp_path / "config"))
    cfg.paths.config_dir.mkdir()
    folder = make_files(cfg.paths.music_dir, "Download")
    monkeypatch.setattr(
        mb_lookup, "fetch_release", Mock(side_effect=AssertionError("the release is stored"))
    )
    app = create_app(cfg)
    # After create_app, which opens its own activity store.
    activity_store.store_release(MBID, mb_cache._key(mb_lookup.RELEASE_INCLUDES), current)
    return TestClient(app, headers={"HX-Request": "true"}), folder


@pytest.fixture
def client(tmp_path, monkeypatch):
    return _app(tmp_path, monkeypatch, release(urls=(LABEL,)))


def _dismissal_panel(client):
    return BeautifulSoup(client.get(f"/library/{MBID}/contributions/editions").text, "html.parser")


def _dismiss(client, page):
    box = page.select_one("input.release-accepted")
    return client.post(
        f"/library/{MBID}/release-accepted",
        data={**json.loads(box["hx-vals"]), "accept": "true"},
    )


def test_provisional_cd_match_reopens_for_new_sibling_and_can_be_dismissed_again(
    tmp_path, monkeypatch
):
    current = release(("CD",))
    client, folder = _app(tmp_path, monkeypatch, current)
    siblings: list[Release] = []
    browse = Mock(
        side_effect=lambda **kw: {
            "release-list": [current, *siblings],
            "release-count": 1 + len(siblings),
        }
    )
    monkeypatch.setattr(musicbrainzngs, "browse_releases", browse)
    old_page = _dismissal_panel(client)
    assert _dismiss(client, old_page).status_code == 200
    assert _dismissal_panel(client).select_one("input.release-accepted").has_attr("checked")
    siblings.append(release(mbid="new-digital"))
    page = _dismissal_panel(client)
    assert not page.select_one("input.release-accepted").has_attr("checked")
    # A stale page cannot silently dismiss the candidate just discovered.
    assert _dismiss(client, old_page).status_code == 409
    assert _dismiss(client, page).status_code == 200
    siblings[0]["title"] = "An unrelated title correction"
    page = _dismissal_panel(client)
    assert page.select_one("input.release-accepted").has_attr("checked")
    # Keeping the CD provisionally never invites adding a download URL to it.
    assert page.select_one('a[href$="/edit"]') is None
    assert sidecar.read(folder).mb_release_id == MBID
    assert sidecar.read(folder).accepted_release_id is None


def test_toggling_the_mismatch_warning_is_not_reported(tmp_path, monkeypatch):
    """The checkbox shows where the setting stands, so neither the Activity feed
    nor the status bar repeats it (#911). The audit log still has the decision."""
    current = release(("CD",))
    client, folder = _app(tmp_path, monkeypatch, current)
    monkeypatch.setattr(
        musicbrainzngs,
        "browse_releases",
        Mock(side_effect=lambda **kw: {"release-list": [current], "release-count": 1}),
    )
    page = _dismissal_panel(client)
    feed_before = activity_store.recent(50, source=activity_store.Source.ACTIVITY)

    responses = [
        _dismiss(client, page),
        _dismiss(client, page),
        client.post(f"/library/{MBID}/release-accepted", data={"mbid": MBID}),
    ]

    assert [r.status_code for r in responses] == [200, 200, 200]
    for r in responses:
        assert "harmonist-status" not in r.headers.get("HX-Trigger", "")
    assert activity_store.recent(50, source=activity_store.Source.ACTIVITY) == feed_before
    audit_rows = activity_store.recent(50, source=activity_store.Source.AUDIT)
    assert len([e for e in audit_rows if "mismatch_dismissal=" in e.message]) == 2
    assert sidecar.read(folder).mismatch_dismissal is None


def test_existing_sibling_gaining_source_evidence_renews_dismissal(client, monkeypatch):
    client, _ = client
    sibling = release(mbid="sibling")
    browse = Mock(
        side_effect=lambda **kw: {
            "release-list": [
                release(urls=(LABEL,)),
                sibling,
                release(urls=(RENAMED,), mbid="other"),
            ],
            "release-count": 3,
        }
    )
    monkeypatch.setattr(musicbrainzngs, "browse_releases", browse)
    old_page = _dismissal_panel(client)
    assert _dismiss(client, old_page).status_code == 200
    sibling["url-relation-list"] = release(urls=(URL,))["url-relation-list"]
    page = _dismissal_panel(client)
    assert not page.select_one("input.release-accepted").has_attr("checked")
    assert _dismiss(client, old_page).status_code == 409
    assert _dismiss(client, page).status_code == 200
    assert _dismissal_panel(client).select_one("input.release-accepted").has_attr("checked")


def test_background_check_keeps_looking_after_provisional_dismissal(tmp_path, monkeypatch):
    current = release(("CD",))
    client, folder = _app(tmp_path, monkeypatch, current)
    browse = Mock(return_value={"release-list": [current], "release-count": 1})
    monkeypatch.setattr(musicbrainzngs, "browse_releases", browse)
    assert _dismiss(client, _dismissal_panel(client)).status_code == 200
    albums = client.app.state.scan_runner.scan_now()
    monkeypatch.setattr(mb_lookup, "fetch_release", Mock(return_value=current))
    monkeypatch.setattr(gardener, "refresh_flag", lambda a, r: gardener.Assessment(None, None, a))
    browse.return_value = {
        "release-list": [current, release(mbid="new-digital")],
        "release-count": 2,
    }
    browse.reset_mock()
    result = gardener.sweep(albums, recheck_after=timedelta(0), limit=1)
    albums = [a.album for a in result.assessments]
    assert browse.call_count == 1
    assert contributions.possible_mismatch(contributions.assess(albums[0]))
    assert sidecar.read(folder).mb_release_id == MBID


def test_incomplete_or_failed_check_preserves_dismissal_without_offering_edits(client, monkeypatch):
    client, folder = client
    sibling = release(urls=(URL,), mbid="sibling")
    browse = Mock(return_value={"release-list": [release(), sibling], "release-count": 2})
    monkeypatch.setattr(musicbrainzngs, "browse_releases", browse)
    assert _dismiss(client, _dismissal_panel(client)).status_code == 200
    before = sidecar.read(folder).mismatch_dismissal
    for payload in (
        {"release-list": [release(), sibling], "release-count": 101},
        {"release-list": [release(("",), mbid="unknown")], "release-count": 1},
    ):
        browse.return_value = payload
        page = _dismissal_panel(client)
        assert page.select_one("input.release-accepted").has_attr("checked")
        assert page.select_one('a[href$="/edit"]') is None
        assert _dismiss(client, page).status_code == 409
    browse.side_effect = musicbrainzngs.NetworkError("offline")
    page = _dismissal_panel(client)
    assert page.select_one('[role="alert"]') is not None
    assert sidecar.read(folder).mismatch_dismissal == before
    # Even a failed comparison retains the control to restore warnings.
    assert client.post(f"/library/{MBID}/release-accepted", data={"mbid": MBID}).status_code == 200
    assert sidecar.read(folder).mismatch_dismissal is None


def test_new_barcode_evidence_but_not_reordering_or_removal_renews_dismissal():
    a = album()
    a.source_upc = "0801061000332"
    first = release(mbid="first")
    other = release(mbid="other")
    assessment = observed(a, siblings=[first, other], formats=("CD",))
    a.sidecar = replace(a.sidecar, mismatch_dismissal=assessment.dismissal)
    assert observed(a, siblings=[other, first], formats=("CD",)).accepted
    assert observed(a, siblings=[first], formats=("CD",)).accepted
    first["barcode"] = "801061000332"
    assert not observed(a, siblings=[first], formats=("CD",)).accepted
    # An existing explicit acceptance remains an explicit acceptance.
    a.sidecar = replace(a.sidecar, accepted_release_id=MBID, mismatch_dismissal=None)
    assert observed(a, siblings=[first], formats=("CD",)).accepted


def test_dismissal_roundtrip_rematch_and_conflicting_folders(tmp_path):
    folder = tmp_path / "album"
    folder.mkdir()
    bookmark = MismatchDismissal(MBID, ("candidate:media",))
    original = Sidecar(mb_release_id=MBID, mismatch_dismissal=bookmark)
    sidecar.write(folder, original)
    loaded = sidecar.read(folder)
    assert loaded is not None
    assert loaded.mismatch_dismissal == bookmark
    assert scanner._merge_sidecars([loaded, loaded], MBID).mismatch_dismissal == bookmark
    other = replace(loaded, mismatch_dismissal=MismatchDismissal(MBID))
    assert scanner._merge_sidecars([loaded, other], MBID).mismatch_dismissal is None
    assert scanner._merge_sidecars([loaded, None], MBID).mismatch_dismissal is None
    sidecar.write(folder, replace(loaded, mb_release_id="different"))
    assert sidecar.read(folder).mismatch_dismissal is None
    rematched = sidecar.read(folder)
    assert rematched is not None
    sidecar.write(folder, replace(rematched, mb_release_id=MBID))
    assert "mismatch_dismissal" not in json.loads(sidecar.sidecar_path(folder).read_text())
    assert any("mismatch_dismissal=" in e.message for e in activity_store.recent(20))


def test_origin_change_clears_dismissal_but_same_origin_does_not(client, monkeypatch):
    client, folder = client
    browse = Mock(return_value={"release-list": [release(urls=(LABEL,))], "release-count": 1})
    monkeypatch.setattr(musicbrainzngs, "browse_releases", browse)
    # Establish the mismatch through another release's store link.
    browse.return_value["release-list"].append(release(urls=(RENAMED,), mbid="sibling"))
    browse.return_value["release-count"] = 2
    assert _dismiss(client, _dismissal_panel(client)).status_code == 200
    assert client.post(f"/library/{MBID}/origin", data={"origin": "automatic"}).status_code == 200
    assert sidecar.read(folder).mismatch_dismissal is not None
    assert client.post(f"/library/{MBID}/origin", data={"origin": "CD"}).status_code == 200
    assert sidecar.read(folder).mismatch_dismissal is None


def test_dismissal_writes_and_clears_every_folder_without_copying_other_decisions(
    client, monkeypatch
):
    client, folder = client
    second = make_files(folder.parent, "Second")
    tags = MP4(second / "track.m4a")
    tags["----:com.apple.iTunes:MusicBrainz Release Track Id"] = [b"second-track"]
    tags.save()
    second_sc = sidecar.read(second)
    assert second_sc is not None
    sidecar.write(second, replace(second_sc, notes="Second folder's note"))
    monkeypatch.setattr(
        musicbrainzngs,
        "browse_releases",
        Mock(
            return_value={
                "release-list": [release(urls=(LABEL,)), release(urls=(RENAMED,), mbid="sibling")],
                "release-count": 2,
            }
        ),
    )
    assert _dismiss(client, _dismissal_panel(client)).status_code == 200
    assert sidecar.read(folder).mismatch_dismissal is not None
    assert sidecar.read(second).mismatch_dismissal == sidecar.read(folder).mismatch_dismissal
    assert sidecar.read(second).notes == "Second folder's note"
    assert sidecar.read(folder).notes is None
    assert client.post(f"/library/{MBID}/release-accepted", data={"mbid": MBID}).status_code == 200
    assert all(sidecar.read(p).mismatch_dismissal is None for p in (folder, second))


@pytest.mark.parametrize(
    "bad",
    [[], {}, {"release": MBID, "evidence": "not a list"}, {"release": MBID, "evidence": [123]}],
)
def test_malformed_dismissal_is_reported(tmp_path, bad):
    with pytest.raises(sidecar.InvalidSidecarError, match="invalid mismatch_dismissal"):
        sidecar._from_dict(
            {"schema_version": 1, "mb_release_id": MBID, "mismatch_dismissal": bad}, tmp_path
        )


def test_dont_warn_records_the_shown_release_once_and_can_be_taken_back(client, monkeypatch):
    client, folder = client
    monkeypatch.setattr(
        musicbrainzngs,
        "browse_releases",
        Mock(
            return_value={
                "release-list": [release(urls=(LABEL,)), release(urls=(RENAMED,), mbid="sibling")],
                "release-count": 2,
            }
        ),
    )
    page = _dismissal_panel(client)
    before = {p: p.read_bytes() for p in folder.iterdir() if p.name != ".harmonist.json"}
    for _ in range(2):
        r = _dismiss(client, page)
        assert r.status_code == 200
        assert sidecar.read(folder).mismatch_dismissal.release == MBID
    # A double click is not a second change.
    rows = [e for e in activity_store.recent(50) if "mismatch_dismissal" in e.message]
    assert len(rows) == 1
    # An unticked box posts no value: that absence takes the acceptance back.
    r = client.post(f"/library/{MBID}/release-accepted", data={"mbid": MBID})
    assert r.status_code == 200 and sidecar.read(folder).mismatch_dismissal is None
    # Never an acceptance of a release the page did not show.
    r = client.post(f"/library/{MBID}/release-accepted", data={"mbid": "other", "accept": "true"})
    assert r.status_code == 409 and sidecar.read(folder).accepted_release_id is None
    assert before == {p: p.read_bytes() for p in before}


def test_panel_renders_both_sections_and_the_checkbox_reveals_one(client, monkeypatch):
    client, folder = client
    sibling = release(urls=(RENAMED,), mbid="sibling")
    current = release(urls=(LABEL,))
    browse = Mock(return_value={"release-list": [current, sibling], "release-count": 2})
    monkeypatch.setattr(musicbrainzngs, "browse_releases", browse)
    for accepted in (False, True):
        if accepted:
            sc = sidecar.read(folder)
            assert sc is not None
            sidecar.write(folder, replace(sc, accepted_release_id=MBID))
        page = BeautifulSoup(
            client.get(f"/library/{MBID}/contributions/editions").text, "html.parser"
        )
        box = page.select_one("input.release-accepted")
        assert box is not None and box.has_attr("checked") is accepted
        # One markup either way: the checkbox shows or hides, nothing re-renders.
        mismatch = page.select_one(f"#mismatch-heading-{MBID}").find_parent("section")
        body = mismatch.select_one(
            "div.group-has-\\[\\.release-accepted\\:checked\\]\\/match\\:hidden"
        )
        assert body is not None and "artist.bandcamp.com" in body.text
        assert "Current match" in body.select('[role="listitem"]')[0].text
        assert "Links artist.bandcamp.com" in body.select('[role="listitem"]')[1].text
        assert harmony_link(body) is not None
        shown = before_dismissal(page)
        assert shown.select_one('a[href$="/edit"]') is None
        contribution = page.select_one(f"#contribution-heading-{MBID}").find_parent("section")
        assert contribution is not None and "hidden" in contribution["class"]
        assert "Store URL missing from this release" in contribution.text
    assert browse.call_count == 2


def test_incomplete_check_flags_nothing_but_keeps_an_acceptance_reversible(client, monkeypatch):
    client, folder = client
    unknown = release(("",), mbid="unknown-media")
    sibling = release(urls=(URL,), mbid="sibling")  # would be a source match
    browse = Mock(
        return_value={
            "release-list": [release(urls=(LABEL,)), sibling, unknown],
            "release-count": 3,
        }
    )
    monkeypatch.setattr(musicbrainzngs, "browse_releases", browse)
    for accepted in (False, True):
        if accepted:
            sc = sidecar.read(folder)
            assert sc is not None
            sidecar.write(folder, replace(sc, accepted_release_id=MBID))
        page = BeautifulSoup(
            client.get(f"/library/{MBID}/contributions/editions").text, "html.parser"
        )
        assert "The check is incomplete" in page.text
        assert page.select_one(f"#mismatch-heading-{MBID}") is None
        assert page.select_one('a[href$="/edit"]') is None
        assert page.select('[role="listitem"]') == []
        box = page.select_one("input.release-accepted")
        assert (box is not None) is accepted
        assert box is None or box.has_attr("checked")


def test_dismissed_warning_with_nothing_to_add_stays_reversible(tmp_path, monkeypatch):
    client, folder = _app(tmp_path, monkeypatch, release(("CD",), urls=(URL,)))
    sc = sidecar.read(folder)
    assert sc is not None
    sidecar.write(folder, replace(sc, accepted_release_id=MBID))
    monkeypatch.setattr(
        musicbrainzngs,
        "browse_releases",
        Mock(return_value={"release-list": [release(("CD",), urls=(URL,))], "release-count": 1}),
    )
    page = BeautifulSoup(client.get(f"/library/{MBID}/contributions/editions").text, "html.parser")
    # The warning folds to its heading, where the tick can still be taken back,
    # and there is no contribution to reveal.
    mismatch = page.select_one(f"#mismatch-heading-{MBID}").find_parent("section")
    assert mismatch is not None
    box = mismatch.select_one("input.release-accepted")
    assert box is not None and box.has_attr("checked")
    assert page.select_one(f"#contribution-heading-{MBID}") is None


def test_album_page_browse_is_stored_for_the_filters_without_another_request(client, monkeypatch):
    client, _ = client
    sibling = release(urls=(RENAMED,), mbid="sibling")
    browse = Mock(return_value={"release-list": [sibling], "release-count": 1})
    monkeypatch.setattr(musicbrainzngs, "browse_releases", browse)
    client.get(f"/library/{MBID}/contributions/editions")
    assert browse.call_count == 1
    stored = mb_cache.stored_release_group_editions(release()["release-group"]["id"])
    assert stored is not None and [r["id"] for r in stored[0]] == ["sibling"]
    albums = client.app.state.scan_runner.scan_now()
    view = _library_page_vars(albums, 1, 30, filter_="possible-mismatch")
    assert [a.title for a in view["rows"]] == ["Download"]
    assert browse.call_count == 1


def test_confirmed_use_accepts_the_chosen_release(tmp_path, monkeypatch):
    from test.test_gardener import _release
    from test.test_web import _confirmation_fields

    client, folder = _app(tmp_path, monkeypatch, release())
    monkeypatch.setattr(mb_lookup, "fetch_release", Mock(return_value=_release(mbid="digital")))
    query = f"replacement={MBID}:digital"
    editor = client.get(f"/assignments/{MBID}?{query}&cancel=true&on_album_page=true")
    applied = client.post(f"/confirm/{MBID}/accept?{query}", data=_confirmation_fields(editor.text))
    assert "confirmation-applied" in applied.headers.get("HX-Trigger", "")
    sc = sidecar.read(folder)
    assert sc.mb_release_id == sc.accepted_release_id == "digital"


def test_update_check_browses_a_group_only_while_it_could_place_an_album(monkeypatch):
    names = ("flagged", "accepted", "accepted-unbrowsed", "accepted-incomplete", "clean")
    albums = {name: album(mbid=name) for name in names}
    for name in names:
        if name.startswith("accepted"):
            albums[name].sidecar = replace(albums[name].sidecar, accepted_release_id=name)
    fetches = {
        name: release(urls=(URL if name == "clean" else LABEL,), mbid=name) for name in names
    }
    for name, payload in fetches.items():
        payload["release-group"] = {"id": f"rg-{name}"}
    # A complete stored comparison settles an accepted release; an incomplete one
    # (a release of unspecified media) is retried.
    key = mb_cache._group_key()
    activity_store.store_release("rg-accepted", key, {"release-list": [], "release-count": 0})
    unknown = release(("",), mbid="unknown-media")
    activity_store.store_release(
        "rg-accepted-incomplete", key, {"release-list": [unknown], "release-count": 1}
    )
    monkeypatch.setattr(mb_lookup, "fetch_release", lambda mbid: fetches[mbid])
    monkeypatch.setattr(gardener, "refresh_flag", lambda a, r: gardener.Assessment(None, None, a))
    browse = Mock(return_value={"release-list": [], "release-count": 0})
    monkeypatch.setattr(musicbrainzngs, "browse_releases", browse)
    result = gardener.sweep(list(albums.values()), recheck_after=timedelta(0), limit=len(names))
    observed = {a.album.revision: a.album for a in result.assessments}
    albums = {name: observed.get(a.revision, a) for name, a in albums.items()}
    browsed = {c.kwargs["release_group"] for c in browse.call_args_list}
    assert browsed == {"rg-flagged", "rg-accepted-unbrowsed", "rg-accepted-incomplete"}
    assert browse.call_count == 3
    assert albums["flagged"].contribution_observation.group == ()
