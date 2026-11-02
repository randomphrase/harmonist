"""A suggested release is reviewed in a dialog, not inline (#933).

The card and the album page say a suggestion is waiting; the review itself opens
in the page's modal mount. What closing the dialog means depends on how it was
opened, and the dialog carries that from the route, so it is asserted here. The
dialog's lifecycle (opening, Esc, the discard on close) is the browser's, and
lives in `test/e2e/test_review_dialog.py`.
"""

from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup

from harmonist import sidecar
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


@pytest.mark.parametrize("on_album_page", [False, True])
def test_waiting_suggestion_is_reviewed_in_a_dialog(client, cfg, monkeypatch, on_album_page):
    d, *_ = _confirmation_setup(cfg, monkeypatch, old_mbid=None)
    aid = _id_for(cfg, d)
    page = client.get(f"/album/{aid}" if on_album_page else "/tasks")
    flag = "true" if on_album_page else "false"
    review = _soup(page.text).select_one(f'button[hx-get="/review/{aid}?on_album_page={flag}"]')
    assert review is not None
    assert review["hx-target"] == "#modal"

    dialog = _dialog(client.get(review["hx-get"]))
    # The editor loads into the dialog, read-only first, exactly as it did inline.
    loader = dialog.select_one("[hx-trigger='load']")
    assert loader is not None
    assert loader["hx-get"] == f"/assignments/{aid}?cancel=true&on_album_page={flag}"
    # Opened from the notice: closing leaves the suggestion waiting.
    assert not dialog.has_attr("data-discard-on-close")


def test_a_chosen_review_is_discarded_when_closed(client, cfg, monkeypatch):
    d, *_ = _confirmation_setup(cfg, monkeypatch, old_mbid=None)
    aid = _id_for(cfg, d)
    dialog = _dialog(client.get(f"/review/{aid}?chosen=true"))
    assert dialog["data-discard-on-close"] == f"/reject/{aid}"


def test_nothing_to_review_opens_no_dialog(client, cfg):
    root = test_web._make_album(cfg, "NoSuggestion")
    aid = _id_for(cfg, root)
    response = client.get(f"/review/{aid}")
    assert response.status_code == 400
    assert "dialog" not in response.text


def _use(monkeypatch):
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release",
        lambda selected: test_web._release_for_match(selected, n_tracks=1),
    )
    monkeypatch.setattr("harmonist.cover_art.front_image", lambda *args, **kwargs: None)
    monkeypatch.setattr("harmonist.contributions.warm", lambda album: None)


def test_use_in_the_inbox_opens_the_chosen_review(client, cfg, monkeypatch):
    import json

    root = test_web._make_album(cfg, "ChooseInInbox")
    aid = _id_for(cfg, root)
    _use(monkeypatch)
    response = client.post(f"/manual/{aid}/assign", data={"mbid": MBID})
    assert response.status_code == 200
    assert sidecar.read(root).mb_match_candidate.mb_release_id == MBID
    # Writing the suggestion can be the album's first sidecar, which moves its
    # id; the review must open on the id the album has now.
    trigger = json.loads(response.headers["HX-Trigger"])
    assert trigger["review-chosen"] == {"album": _id_for(cfg, root)}
    assert "HX-Redirect" not in response.headers


def test_use_on_the_album_page_reopens_it_with_the_chosen_review(client, cfg, monkeypatch):
    root = test_web._make_album(cfg, "ChooseOnPage")
    aid = _id_for(cfg, root)
    _use(monkeypatch)
    response = client.post(f"/manual/{aid}/assign", data={"mbid": MBID, "on_album_page": "true"})
    destination = urlsplit(response.headers["HX-Redirect"])
    aid = _id_for(cfg, root)
    assert destination.path == f"/album/{aid}"
    assert parse_qs(destination.query) == {"review": ["chosen"]}

    page = _soup(client.get(response.headers["HX-Redirect"]).text)
    opener = page.select_one('[hx-get^="/review/"][hx-trigger="load"]')
    assert opener is not None
    assert opener["hx-get"] == f"/review/{aid}?chosen=true&on_album_page=true"
    assert opener["hx-target"] == "#modal"


def test_the_album_page_opens_no_review_once_the_suggestion_is_resolved(client, cfg):
    # A reload of `?review=chosen` after the suggestion was confirmed or
    # dismissed elsewhere has nothing to open.
    root = test_web._make_album(cfg, "Resolved")
    aid = _id_for(cfg, root)
    page = _soup(client.get(f"/album/{aid}?review=chosen").text)
    assert page.select_one('[hx-get^="/review/"][hx-trigger="load"]') is None
