"""A release is reviewed in the Review dialog, from a release row (#933).

A suggestion Harmonist found is stored, and shown as a release row like any
search result. A release the user picks is reviewed without storing anything:
only Confirm writes. Both open the same dialog in the page's modal mount. The
dialog's lifecycle is the browser's, in `test/e2e/test_review_dialog.py`.
"""

import json
from dataclasses import replace

import pytest
from bs4 import BeautifulSoup

from harmonist import sidecar
from harmonist.models import FoundBy
from test import test_web
from test.test_web import _confirmation_setup, _id_for

cfg = test_web.cfg
client = test_web.client

MBID = "abc12345-1234-1234-1234-1234567890ab"


def _soup(html):
    return BeautifulSoup(html, "html.parser")


def _dialog(response):
    assert response.status_code == 200, response.text
    dialog = _soup(response.text).select_one("dialog.review-dialog")
    assert dialog is not None
    return dialog


def _loader(dialog):
    loader = dialog.select_one("[hx-trigger='load']")
    assert loader is not None
    return loader["hx-get"]


@pytest.mark.parametrize("on_album_page", [False, True])
def test_a_waiting_suggestion_is_a_release_row_with_its_evidence(
    client, cfg, monkeypatch, on_album_page
):
    d, *_ = _confirmation_setup(cfg, monkeypatch, old_mbid=None)
    current = sidecar.read(d)
    assert current is not None and current.mb_match_candidate is not None
    sidecar.write(
        d,
        replace(
            current,
            mb_match_candidate=replace(current.mb_match_candidate, found_by=FoundBy.STORE_URL),
        ),
    )
    aid = _id_for(cfg, d)
    page = _soup(client.get(f"/album/{aid}" if on_album_page else "/tasks").text)
    flag = "true" if on_album_page else "false"
    review = page.select_one(f'button[hx-get="/review/{aid}?on_album_page={flag}"]')
    assert review is not None
    assert review.get_text(strip=True) == "Review"
    assert review["hx-target"] == "#modal"
    # A row in the search's own results, as the lookup that found it would have
    # listed it, saying how; a wrong one is answered by searching right there.
    row = review.find_parent(attrs={"role": "listitem"})
    assert row is not None and "release-candidate" in row["class"]
    assert row["aria-label"] == "Suggested release"
    assert row.find_parent(id=f"mbid-results-{aid}") is not None
    assert "Store URL matches" in row.get_text(" ", strip=True)
    assert row.select_one('a[href="https://musicbrainz.org/release/rel-new-confirm"]')
    # The mode that found it is the one selected.
    mode = page.select_one(f'input[name="find-{aid}"][value="url"]')
    assert mode is not None and mode.has_attr("checked")

    dialog = _dialog(client.get(review["hx-get"]))
    assert _loader(dialog) == f"/assignments/{aid}?cancel=true&on_album_page={flag}"


def test_a_search_that_finds_the_suggestion_lists_it_once(client, cfg, monkeypatch):
    """The suggestion is the search's own row, not a second one beside it."""
    from harmonist import mb_lookup

    d, release, *_ = _confirmation_setup(cfg, monkeypatch, old_mbid=None)
    aid = _id_for(cfg, d)
    summary = mb_lookup.release_summary(release)
    other = dict(summary, id="rel-other", title="Another")
    monkeypatch.setattr(
        "harmonist.mb_search.search_releases",
        lambda *args, **kwargs: test_web._searched(other, summary),
    )
    response = client.post(f"/manual/{aid}/search", data={"artist": "A", "title": "T"})
    rows = _soup(response.text).select('[role="listitem"]')
    assert len(rows) == 2
    # It leads, marked, and opens as the stored suggestion, where Dismiss is.
    assert rows[0]["aria-label"] == "Suggested release"
    review = rows[0].select_one("button[hx-get]")
    assert review is not None and review["hx-get"] == f"/review/{aid}?on_album_page=false"
    assert not rows[1].has_attr("aria-label")


def test_nothing_to_review_opens_no_dialog(client, cfg):
    root = test_web._make_album(cfg, "NoSuggestion")
    aid = _id_for(cfg, root)
    response = client.get(f"/review/{aid}")
    assert response.status_code == 400
    assert "dialog" not in response.text


def _search(client, monkeypatch, aid, *, on_album_page=False):
    from harmonist import mb_lookup

    release = test_web._release_for_match(MBID, n_tracks=1)
    monkeypatch.setattr(
        "harmonist.mb_search.search_releases",
        lambda *args, **kwargs: test_web._searched(mb_lookup.release_summary(release)),
    )
    response = client.post(
        f"/manual/{aid}/search",
        data={"artist": "Artist", "title": "Title", "on_album_page": str(on_album_page).lower()},
    )
    assert response.status_code == 200
    return _soup(response.text)


@pytest.mark.parametrize("on_album_page", [False, True])
def test_a_search_result_opens_its_review_and_stores_nothing(
    client, cfg, monkeypatch, on_album_page
):
    root = test_web._make_album(cfg, "Chosen")
    aid = _id_for(cfg, root)
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release",
        lambda selected: test_web._release_for_match(selected, n_tracks=1),
    )
    results = _search(client, monkeypatch, aid, on_album_page=on_album_page)
    review = results.select_one("button[hx-get^='/review/']")
    assert review is not None
    assert review.get_text(strip=True) == "Review"
    assert review["hx-target"] == "#modal"
    flag = "true" if on_album_page else "false"
    assert review["hx-get"] == (
        f"/review/{aid}?release={MBID}&found_by=name_search&on_album_page={flag}"
    )

    dialog = _dialog(client.get(review["hx-get"]))
    assert not dialog.has_attr("data-discard-on-close")
    editor = client.get(_loader(dialog))
    assert editor.status_code == 200, editor.text
    confirm = _soup(editor.text).select_one("button[hx-post*='/confirm/']")
    assert confirm is not None and confirm.get_text(strip=True) == "Confirm suggestion"
    assert f"release={MBID}" in confirm["hx-post"]
    assert "found_by=name_search" in confirm["hx-post"]
    # Opening and reading the review wrote nothing: the album is as it was.
    assert sidecar.read(root) is None


def test_a_suggestion_waiting_does_not_stop_a_search(client, cfg, monkeypatch):
    d, *_ = _confirmation_setup(cfg, monkeypatch, old_mbid=None)
    aid = _id_for(cfg, d)
    results = _search(client, monkeypatch, aid)
    assert results.select_one(f"button[hx-get^='/review/{aid}?release={MBID}']") is not None
    # Reviewing another release leaves Harmonist's suggestion where it was.
    client.get(f"/review/{aid}?release={MBID}&found_by=name_search")
    assert sidecar.read(d).mb_match_candidate.mb_release_id == "rel-new-confirm"


def test_a_pasted_mbid_opens_its_review(client, cfg, monkeypatch):
    root = test_web._make_album(cfg, "Pasted")
    aid = _id_for(cfg, root)
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release",
        lambda selected: test_web._release_for_match(selected, n_tracks=1),
    )
    page = _soup(client.get("/tasks").text)
    paste = page.select_one(f'form[hx-get="/review/{aid}"]')
    assert paste is not None
    assert paste["hx-target"] == "#modal"
    fields = {i["name"]: i.get("value", "") for i in paste.select("input[name]")}
    assert fields["found_by"] == "mbid"
    assert "release" in fields

    url = f"https://musicbrainz.org/release/{MBID}"
    dialog = _dialog(client.get(f"/review/{aid}", params={"release": url, "found_by": "mbid"}))
    assert f"release={MBID}" in _loader(dialog)
    assert sidecar.read(root) is None


def test_a_chosen_releases_archive_image_is_served(client, cfg, monkeypatch):
    """The review shows the chosen release's cover from the archive, which is
    named by the review's query and stored nowhere else."""
    d, release, *_ = _confirmation_setup(cfg, monkeypatch, old_mbid=None)
    current = sidecar.read(d)
    assert current is not None
    sidecar.write(d, replace(current, mb_match_candidate=None))
    aid = _id_for(cfg, d)
    query = {"release": release["id"], "found_by": "mbid"}
    editor = client.get(f"/assignments/{aid}", params=query | {"cancel": "true"})
    fingerprint = test_web._confirmation_fields(editor.text)["release_fingerprint"]
    artwork = client.get(
        f"/assignments/{aid}/artwork", params=query | {"release_fingerprint": fingerprint}
    )
    image = _soup(artwork.text).select_one('img[alt="Artwork for selected release"]')
    assert image is not None
    assert client.get(image["src"]).status_code == 200


def test_an_unreadable_mbid_says_so_without_a_dialog(client, cfg):
    root = test_web._make_album(cfg, "Garbled")
    aid = _id_for(cfg, root)
    response = client.get(f"/review/{aid}", params={"release": "not an id", "found_by": "mbid"})
    assert response.status_code == 200
    assert "dialog" not in response.text
    assert response.headers["HX-Reswap"] == "none"
    status = json.loads(response.headers["HX-Trigger"])["harmonist-status"]
    assert status["verb"] == "Could not parse"
