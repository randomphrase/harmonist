"""Artist NFOs without an ID wait in the Inbox, with hints from their folders
(#996)."""

from __future__ import annotations

import json
import shutil
from unittest.mock import Mock

import pytest
from bs4 import BeautifulSoup

from harmonist import id_registry, mb_cache, mb_lookup
from harmonist.artists import Hint, Hints
from test.test_artist_web import ALICE, BOB, artist_client, make_album

__all__ = ["artist_client"]

LIVE = "66666666-6666-4666-8666-666666666666"
UNCONFIRMED = "88888888-8888-4888-8888-888888888888"
ALICE_MB = {"id": ALICE, "name": "Alice", "sort-name": "Alice", "type": "Person"}


@pytest.fixture
def inbox(artist_client, monkeypatch):
    """An empty artist.nfo in Alice's folder, above one Library album crediting
    her and one Inbox album crediting Bob; MusicBrainz answering for Alice."""
    client, cfg, network = artist_client
    music = cfg.paths.music_dir
    make_album(music, "Alice/Live", LIVE, ["Alice"], [ALICE])
    make_album(music, "Alice/Unconfirmed", UNCONFIRMED, ["Bob"], [BOB], confirmed=False)
    path = music / "Alice" / "artist.nfo"
    path.write_bytes(b"")
    lookup = Mock(side_effect=lambda mbid: dict(ALICE_MB))
    monkeypatch.setattr(mb_lookup, "fetch_artist", lookup)
    # The Inbox albums carry release IDs, which would start a reconcile.
    monkeypatch.setattr(client.app.state.reconcile_runner, "start", Mock(return_value=False))
    return client, path, id_registry.get_or_mint(path), lookup, network


def card(html, key):
    return BeautifulSoup(html, "html.parser").find(id=f"artist-task-{key}")


#: The finder's ways to look, each `(value, checked, disabled)`, when the
#: folder offers artists, and when it offers none.
FROM_ALBUMS = [("albums", True, False), ("name", False, False), ("mbid", False, False)]
BY_NAME = [("albums", False, True), ("name", True, False), ("mbid", False, False)]


def modes(finder):
    """The finder's ways to look, each `(value, checked, disabled)`."""
    return [
        (r["value"], r.has_attr("checked"), r.has_attr("disabled"))
        for r in finder.find_all("input", attrs={"type": "radio"})
    ]


def test_a_card_lists_its_folders_artists_first(inbox):
    client, _path, key, lookup, network = inbox
    response = client.get("/tasks")
    # The Inbox album beneath the folder isn't in the Library, so Bob is no hint.
    assert [(c.key, c.name, c.hints) for c in response.context["artist_cards"]] == [
        (key, "Alice", Hints(1, (Hint(ALICE, "Alice", 1),)))
    ]
    found = card(response.text, key)
    assert found.find("a", href=f"/artist/{key}") is not None
    finder = found.find(id=f"artist-find-{key}")
    assert modes(finder) == FROM_ALBUMS
    panel = finder.select_one(".find-mode__panel--albums")
    suggestion = panel.find(attrs={"aria-label": "Suggested artist"})
    assert "On 1 of 1 album" in suggestion.get_text(" ", strip=True)
    form = suggestion.find("form", attrs={"hx-post": f"/artist/{key}/identify"})
    assert {i["name"]: i["value"] for i in form.find_all("input")} == {
        "file": key,
        "artist": ALICE,
    }
    # Showing the Inbox asks MusicBrainz nothing.
    assert (lookup.call_count, network.call_count) == (0, 0)


def test_a_folder_with_no_library_albums_searches_by_name(inbox):
    client, path, _key, _lookup, _network = inbox
    other = path.parent.parent / "metadata" / "Nobody" / "artist.nfo"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"")
    key = id_registry.get_or_mint(other)
    finder = card(client.get("/tasks").text, key).find(id=f"artist-find-{key}")
    assert modes(finder) == BY_NAME
    assert finder.select_one(".find-mode__panel--albums") is None


def test_an_artist_is_named_with_its_disambiguation_from_a_stored_release(inbox, monkeypatch):
    client, _path, key, _lookup, network = inbox
    stored = {
        LIVE: {
            "artist-credit": [
                {"artist": {"id": ALICE, "name": "Alice", "disambiguation": "the singer"}}
            ]
        }
    }
    monkeypatch.setattr(mb_lookup, "fetch_artist", Mock(side_effect=AssertionError("network")))
    monkeypatch.setattr(mb_cache, "stored_release", stored.get)
    row = card(client.get("/tasks").text, key).find(attrs={"aria-label": "Suggested artist"})
    link = row.find("a", href=f"https://musicbrainz.org/artist/{ALICE}")
    assert link.get_text(" ", strip=True).startswith("Alice (the singer)")
    assert network.call_count == 0


def test_the_artist_page_offers_the_same_artists(inbox):
    client, _path, key, _lookup, _network = inbox
    page = BeautifulSoup(client.get(f"/artist/{key}").text, "html.parser")
    finder = page.find(id=f"artist-find-{key}")
    assert modes(finder) == FROM_ALBUMS
    chosen = page.select_one(f"#artist-find-{key} [aria-label='Suggested artist'] [name=artist]")
    assert chosen is not None
    assert chosen["value"] == ALICE


def test_an_identified_nfo_has_no_card(inbox):
    client, path, key, _lookup, _network = inbox
    path.write_text(f"<artist><musicbrainzartistid>{ALICE}</musicbrainzartistid></artist>")
    response = client.get("/tasks")
    assert response.context["artist_cards"] == []
    assert card(response.text, key) is None


def test_the_inbox_counts_each_card(inbox):
    client, path, _key, _lookup, _network = inbox
    with_card = client.get("/status").json()["counts"]["inbox"]
    path.unlink()
    assert client.get("/status").json()["counts"]["inbox"] == with_card - 1


def test_a_card_is_listed_when_no_album_needs_attention(inbox):
    client, path, key, _lookup, _network = inbox
    music = path.parent.parent
    shutil.rmtree(music / "Inbox only")
    shutil.rmtree(music / "Alice" / "Unconfirmed")
    response = client.get("/tasks")
    assert response.context["albums"] == []
    assert card(response.text, key) is not None
    assert "Inbox empty" not in response.text


def test_identify_from_the_inbox_stays_in_the_inbox(inbox):
    client, path, key, lookup, _network = inbox
    response = client.post(
        f"/artist/{key}/identify",
        data={"file": key, "artist": ALICE},
        headers={"HX-Current-URL": "http://testserver/?tab=inbox"},
    )
    assert response.status_code == 200
    # The card leaves as a confirmed album's does, rather than the browser
    # going to the artist page.
    assert "HX-Redirect" not in response.headers
    assert json.loads(response.headers["HX-Trigger"])["tasks-changed"] is True
    lookup.assert_called_once_with(ALICE)
    assert f"<musicbrainzartistid>{ALICE}</musicbrainzartistid>" in path.read_text()
    assert client.get("/tasks").context["artist_cards"] == []


def test_identify_on_the_artist_page_follows_the_file_to_its_new_key(inbox):
    client, _path, key, _lookup, _network = inbox
    response = client.post(
        f"/artist/{key}/identify",
        data={"file": key, "artist": ALICE},
        headers={"HX-Current-URL": f"http://testserver/artist/{key}?artist_page=2"},
    )
    assert response.headers["HX-Redirect"] == f"/artist/{ALICE}?artist_page=2"
