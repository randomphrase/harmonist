"""A CD origin checks media; its UPC, when present, also checks identity."""

from dataclasses import replace
from unittest.mock import Mock

import musicbrainzngs
import pytest
from fastapi.testclient import TestClient

from harmonist import activity_store, contributions, mb_cache, mb_lookup, scanner, sidecar
from harmonist.config import Config, PathsConfig
from harmonist.provenance import Origin
from harmonist.web.main import create_app
from test.helpers import ACCURATERIP_TAGS, write_provenance_tags
from test.test_contributions import MBID, release
from test.test_release_match import _app
from test.test_upc_contributions import OTHER_UPC, UPC, download
from test.test_web import _confirmation_fields


def rip(tmp_path, upcs=(UPC,)):
    """A CD rip whose ripper wrote a UPC: AccurateRip's tags, no store's mark."""
    folder = download(tmp_path, upcs, marks={})
    write_provenance_tags(folder, ".m4a", ACCURATERIP_TAGS)
    return folder


def edition(formats, barcode, mbid):
    r = release(formats, mbid=mbid)
    r["barcode"] = barcode
    return r


def assessed(folder, matched, *siblings):
    """The album matched to `matched`, with its group — `matched` and `siblings`
    — browsed, so the Library's filters have everything they need."""
    album = scanner.scan(folder.parent)[0]
    contributions.observe(album, matched, None)
    contributions.observe_group(album, [matched, *siblings], 1 + len(siblings))
    return contributions.assess(album)


def reasons(assessment):
    assert assessment.siblings is not None
    shown = contributions.panel(assessment, assessment.siblings)
    assert shown is not None
    return shown


def test_another_cd_carrying_the_rips_upc_is_a_possible_mismatch(tmp_path):
    matched = edition(("CD",), OTHER_UPC, MBID)
    sibling = edition(("CD",), UPC, "sibling-cd")
    assessment = assessed(rip(tmp_path), matched, sibling)
    assert contributions.possible_mismatch(assessment)
    assert reasons(assessment).reasons == ("source_match", "barcode_different")
    assert [e["id"] for e in assessment.siblings.source_matches] == ["sibling-cd"]


def test_a_rips_candidates_are_physical_releases_not_digital_ones(tmp_path):
    """A download's evidence points at digital releases, a rip's at physical
    ones: a digital release carrying the UPC is no candidate for a CD rip."""
    matched = edition(("CD",), OTHER_UPC, MBID)
    digital = edition(("Digital Media",), UPC, "sibling-digital")
    assessment = assessed(rip(tmp_path), matched, digital)
    assert assessment.siblings.editions == []
    assert reasons(assessment).reasons == ("barcode_different",)


def test_a_rip_matched_to_the_cd_with_its_barcode_is_settled(tmp_path):
    matched = edition(("CD",), UPC, MBID)
    assessment = assessed(rip(tmp_path), matched, edition(("CD",), OTHER_UPC, "sibling-cd"))
    assert assessment.eligible
    assert not contributions.possible_mismatch(assessment)
    assert not contributions.contribution_due(assessment)


def test_a_rips_upc_is_never_offered_to_musicbrainz(tmp_path):
    """The ripper's metadata provider supplied it, not the disc: evidence for
    reviewing the match, never a barcode to add or correct."""
    for barcode in (None, "", OTHER_UPC):
        folder = rip(tmp_path / str(barcode))
        assessment = assessed(folder, edition(("CD",), barcode, MBID))
        shown = reasons(assessment)
        assert shown.finding is None
        assert not contributions.contribution_due(assessment)


def test_a_rip_matched_to_a_digital_release_is_a_possible_mismatch(tmp_path):
    """The media reason turns round: a download matched to a CD is suspect, and
    so is a CD rip matched to a download."""
    assessment = assessed(rip(tmp_path), edition(("Digital Media",), UPC, MBID))
    assert assessment.media_mismatch is True
    assert contributions.possible_mismatch(assessment)
    # Harmony finds releases in digital stores, which can't add a CD.
    assert not reasons(assessment).add_release


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize(
    ("formats", "mismatch"),
    [
        (("Digital Media",), True),
        (("CD",), False),
        (("CD", "Digital Media"), False),
        (("",), None),
        ((), None),
    ],
)
def test_cd_media_can_be_checked_without_an_original_upc(tmp_path, explicit, formats, mismatch):
    folder = download(tmp_path, (None,), marks={}) if explicit else rip(tmp_path, (None,))
    if explicit:
        sc = sidecar.read(folder)
        assert sc is not None
        sidecar.write(folder, replace(sc, origin_override=Origin.CD))
    assessment = assessed(folder, edition(formats, OTHER_UPC, MBID))
    assert assessment.eligible
    assert assessment.rip
    assert assessment.source_upc is None
    assert assessment.barcode_status is None
    assert assessment.media_mismatch is mismatch
    assert contributions.possible_mismatch(assessment) is (mismatch is True)
    assert not contributions.contribution_due(assessment)


def test_a_rips_physical_candidate_opens_for_review(tmp_path):
    """Every candidate offered must be usable: the review that replaces the
    match accepts what the candidates' own rule does, a physical release for a
    rip — it used to accept only digital ones, so Use was a dead end."""
    cfg = Config(paths=PathsConfig(music_dir=tmp_path / "music", config_dir=tmp_path / "config"))
    cfg.paths.config_dir.mkdir()
    activity_store.init(tmp_path / "activity.db")
    rip(cfg.paths.music_dir)
    client = TestClient(create_app(cfg), headers={"HX-Request": "true"})
    for r in (edition(("CD",), OTHER_UPC, MBID), edition(("CD",), UPC, "sibling-cd")):
        activity_store.store_release(r["id"], mb_cache._key(mb_lookup.RELEASE_INCLUDES), r)
    aid = next(a.id for a in client.app.state.scan_runner.scan_now())
    editor = client.get(
        f"/assignments/{aid}?replacement={MBID}:sibling-cd&cancel=true&on_album_page=true"
    )
    assert editor.status_code == 200
    assert _confirmation_fields(editor.text)["candidate_mbid"] == "sibling-cd"


@pytest.mark.parametrize("origin", [Origin.CD, Origin.AMAZON])
def test_store_url_limitations_apply_only_to_downloads(tmp_path, monkeypatch, origin):
    current = release(("Digital Media" if origin is Origin.CD else "CD",))
    client, folder = _app(tmp_path, monkeypatch, current)
    sc = sidecar.read(folder)
    assert sc is not None
    sidecar.write(folder, replace(sc, origin_override=origin))
    browse = Mock(return_value={"release-list": [current], "release-count": 1})
    monkeypatch.setattr(musicbrainzngs, "browse_releases", browse)
    response = client.get(f"/library/{MBID}/contributions/editions")
    assert response.status_code == 200
    assert "Possible mismatch" in response.text
    assert ("The store URL check is incomplete." in response.text) is (origin is Origin.AMAZON)
    assert browse.call_count == 1
