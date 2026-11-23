"""Filling in an artist NFO from MusicBrainz on its artist page, with History
and Undo (#981, #917)."""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from bs4 import BeautifulSoup

from harmonist import activity_store, id_registry, mb_lookup, mb_search, nfo_history, wikimedia
from harmonist.activity_store import Level, Source
from test.test_artist_web import ALICE, artist_client

__all__ = ["artist_client"]

CAROL = "77777777-7777-4777-8777-777777777777"
CAROL_MB = {
    "id": CAROL,
    "name": "Carol",
    "sort-name": "Carol, The",
    "type": "Group",
    "life-span": {"begin": "1999"},
}


@pytest.fixture
def identify(artist_client, monkeypatch):
    """An empty artist.nfo in Carol's folder, and MusicBrainz answering for Carol."""
    client, cfg, _network = artist_client
    path = cfg.paths.music_dir / "Carol" / "artist.nfo"
    path.parent.mkdir()
    path.write_bytes(b"")
    lookup = Mock(side_effect=lambda mbid: dict(CAROL_MB))
    monkeypatch.setattr(mb_lookup, "fetch_artist", lookup)
    return client, path, id_registry.get_or_mint(path), lookup


def look_up(client, key, file_key, pasted=f"https://musicbrainz.org/artist/{CAROL}"):
    return client.get(f"/artist/{key}/lookup", params={"file": file_key, "artist": pasted})


def identify_as(client, key, file_key, artist=CAROL):
    return client.post(f"/artist/{key}/identify", data={"file": file_key, "artist": artist})


def history(client, key):
    page = client.get(f"/artist/{key}")
    return page, page.context["history"], page.context["history_writes"]


def test_a_pasted_mbid_lists_the_artist_and_writes_nothing(identify):
    client, path, key, lookup = identify
    response = look_up(client, key, key)
    assert response.status_code == 200
    assert response.context["results"] == [
        {
            "id": CAROL,
            "name": "Carol",
            "disambiguation": "",
            "type": "Group",
            "area": None,
            "begin": "1999",
            "end": None,
        }
    ]
    rows = BeautifulSoup(response.text, "html.parser").select('[role="listitem"]')
    assert len(rows) == 1
    link = rows[0].select_one(f'a[href="https://musicbrainz.org/artist/{CAROL}"]')
    assert link is not None and "Carol" in link.get_text()
    assert path.read_bytes() == b""
    lookup.assert_called_once_with(CAROL)


def test_something_pasted_that_is_no_mbid_is_said_so(identify):
    client, _path, key, lookup = identify
    response = look_up(client, key, key, "not an id")
    assert "That isn" in response.text and "MusicBrainz artist URL or MBID" in response.text
    lookup.assert_not_called()


def test_identify_writes_what_musicbrainz_says_when_it_is_pressed(identify):
    client, path, key, lookup = identify
    look_up(client, key, key)
    lookup.side_effect = lambda mbid: {**CAROL_MB, "name": "Carol (renamed)"}
    identify_as(client, key, key)
    assert lookup.call_count == 2
    assert "<name>Carol (renamed)</name>" in path.read_text()


def test_the_page_finds_by_name_for_an_unidentified_nfo(identify):
    client, _path, key, _lookup = identify
    page = BeautifulSoup(client.get(f"/artist/{key}").text, "html.parser")
    finder = page.select_one(f"#artist-find-{key}")
    assert finder is not None
    checked = finder.select_one('input[type="radio"][checked]')
    assert checked is not None and checked["value"] == "name"
    search = finder.select_one(f'form[hx-get="/artist/{key}/search"]')
    assert search is not None
    # An empty file has no name, so the folder's stands in, as on the list.
    name = search.select_one('input[name="name"]')
    assert name is not None and name["value"] == "Carol"
    # Always open, so there's nothing to cancel: the file has no artist to
    # go back to.
    assert one(page, "#artist-find").find("a", string="Cancel") is None


EDIT = '#artist-identity a[aria-label^="Change the MusicBrainz artist"]'


def one(node, selector):
    """The element a selector names, which has to be there."""
    found = node.select_one(selector)
    assert found is not None, selector
    return found


def test_an_identified_nfos_edit_button_opens_its_finder(identify):
    """As an album's edit button opens its release search (#938). Its own ID is
    in the MBID field, ready to fill in what the file lacks."""
    client, _path, _key, _lookup = identify
    closed = BeautifulSoup(client.get(f"/artist/{ALICE}").text, "html.parser")
    assert closed.select_one("#artist-find") is None
    edit = one(closed, EDIT)
    assert edit["href"] == edit["hx-get"]

    page = BeautifulSoup(client.get(edit["href"]).text, "html.parser")
    finder = one(page, "#artist-find")
    assert finder.h2.get_text(strip=True) == "Find this artist"
    assert one(finder, 'input[type="radio"][checked]')["value"] == "mbid"
    assert one(finder, 'input[name="artist"]')["value"] == ALICE
    # Nothing was written to open it, so the address is what keeps it open
    # through the page's own redraws, and Cancel is the address without it.
    assert one(page, "#artist-detail")["hx-get"] == edit["href"]
    cancel = finder.find("a", string="Cancel")
    assert cancel is not None
    assert cancel["href"] == one(closed, "#artist-detail")["hx-get"]


def test_a_find_that_names_no_file_here_opens_nothing(identify):
    client, _path, key, _lookup = identify
    page = BeautifulSoup(client.get(f"/artist/{ALICE}", params={"find": key}).text, "html.parser")
    assert page.select_one("#artist-find") is None
    assert "find=" not in one(page, "#artist-detail")["hx-get"]


def test_a_write_from_an_opened_finder_closes_it(identify):
    """As an album's release search goes once a release is confirmed."""
    client, _path, _key, _lookup = identify
    edit = one(BeautifulSoup(client.get(f"/artist/{ALICE}").text, "html.parser"), EDIT)
    file_key = edit["href"].split("find=")[1]
    response = client.post(
        f"/artist/{ALICE}/identify",
        data={"file": file_key, "artist": CAROL},
        headers={"HX-Current-URL": f"http://testserver{edit['href']}"},
    )
    assert response.headers["HX-Redirect"].startswith(f"/artist/{CAROL}?")
    assert "artist_page=1" in response.headers["HX-Redirect"]
    assert "find=" not in response.headers["HX-Redirect"]


def test_a_name_search_lists_artists_each_with_identify(identify, monkeypatch):
    client, _path, key, _lookup = identify
    search = Mock(return_value=([{**_summary(CAROL, "Carol"), "type": "Group"}], 3))
    monkeypatch.setattr(mb_search, "search_artists", search)
    response = client.get(f"/artist/{key}/search", params={"file": key, "name": " Carol "})
    search.assert_called_once_with(" Carol ")
    results = BeautifulSoup(response.text, "html.parser")
    rows = results.select('[role="listitem"]')
    assert len(rows) == 1
    form = rows[0].select_one(f'form[hx-post="/artist/{key}/identify"]')
    assert form is not None
    fields = {field["name"]: field["value"] for field in form.select('input[type="hidden"]')}
    assert fields == {"file": key, "artist": CAROL}
    # A write that fails says why here, so what it names has to be in the results.
    assert results.select_one(str(form["hx-target"])) is not None
    assert "Showing 1 of 3" in response.text


def test_a_failed_search_says_so(identify, monkeypatch):
    client, _path, key, _lookup = identify
    monkeypatch.setattr(
        mb_search, "search_artists", Mock(side_effect=mb_search.MBSearchError("503"))
    )
    response = client.get(f"/artist/{key}/search", params={"file": key, "name": "Carol"})
    assert "search MusicBrainz. Try again later." in response.text


def test_the_artist_search_asks_by_name_and_alias(monkeypatch):
    answer = {
        "artist-list": [
            {
                "id": CAROL,
                "name": "Carol",
                "disambiguation": "the band",
                "type": "Group",
                "area": {"name": "Pawnee"},
                "life-span": {"begin": "1999", "end": "2004", "ended": "true"},
            }
        ],
        "artist-count": "12",
    }
    search = Mock(return_value=answer)
    monkeypatch.setattr(mb_search.musicbrainzngs, "search_artists", search)
    results, total = mb_search.search_artists('Say "Hi"')
    search.assert_called_once_with(query='artist:"Say \\"Hi\\"" OR alias:"Say \\"Hi\\""', limit=10)
    assert results == [
        {
            "id": CAROL,
            "name": "Carol",
            "disambiguation": "the band",
            "type": "Group",
            "area": "Pawnee",
            "begin": "1999",
            "end": "2004",
        }
    ]
    assert total == 12
    assert mb_search.search_artists("  ") == ([], 0)


def _summary(mbid, name):
    return {
        "id": mbid,
        "name": name,
        "disambiguation": "",
        "type": None,
        "area": None,
        "begin": None,
        "end": None,
    }


def test_identify_writes_records_it_and_moves_the_page(identify):
    client, path, key, lookup = identify
    response = identify_as(client, key, key)
    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == f"/artist/{CAROL}"
    lookup.assert_called_once_with(CAROL)
    written = path.read_text()
    assert f"<musicbrainzartistid>{CAROL}</musicbrainzartistid>" in written
    assert "<formed>1999</formed>" in written

    page, events, writes = history(client, CAROL)
    entry = next(e for e in events if e.message.startswith("Identified"))
    assert (entry.artist_id, entry.artist_label) == (CAROL, "Carol")
    write = writes[entry.id]
    assert write.undoable
    assert {(c.field, c.before, c.after) for c in write.changes} >= {
        ("musicbrainzartistid", None, CAROL)
    }
    assert page.context["artist"].mbid == CAROL
    # The old page's History has the write too, and its address leads on.
    moved = client.get(f"/artist/{key}", follow_redirects=False)
    assert (moved.status_code, moved.headers["location"]) == (307, f"/artist/{CAROL}")


def test_the_feed_names_and_links_the_artist(identify):
    client, _path, key, _lookup = identify
    identify_as(client, key, key)
    feed = BeautifulSoup(client.get("/activity").text, "html.parser")
    link = feed.select_one(f'a[href="/artist/{CAROL}"]')
    assert link is not None and link.get_text(strip=True) == "Carol"


def test_a_file_edited_as_it_is_written_is_left_alone(identify, monkeypatch):
    client, path, key, _lookup = identify
    write = nfo_history.write

    def edited_meanwhile(target, **kwargs):
        target.write_text("<artist><name>Edited elsewhere</name></artist>")
        return write(target, **kwargs)

    monkeypatch.setattr(nfo_history, "write", edited_meanwhile)
    response = identify_as(client, key, key)
    assert "just changed" in response.text
    assert "HX-Redirect" not in response.headers
    assert path.read_text() == "<artist><name>Edited elsewhere</name></artist>"
    assert activity_store.artist_history(key) == []


def test_identifying_with_the_same_answer_again_records_nothing(identify):
    client, _path, key, _lookup = identify
    identify_as(client, key, key)
    before = activity_store.artist_history(CAROL)
    again = identify_as(client, CAROL, key)
    assert "Nothing to change" in again.headers["HX-Trigger"]
    assert activity_store.artist_history(CAROL) == before


def test_identify_that_changes_nothing_still_closes_the_finder(identify):
    """The artist is identified either way, so its finder goes, as after a
    write (#1021), and what it says goes to the status bar rather than into the
    finder."""
    client, _path, key, _lookup = identify
    identify_as(client, key, key)
    opened = f"/artist/{CAROL}?artist_page=1&find={key}"
    again = client.post(
        f"/artist/{CAROL}/identify",
        data={"file": key, "artist": CAROL},
        headers={"HX-Current-URL": f"http://testserver{opened}"},
    )
    assert "Nothing to change" in again.headers["HX-Trigger"]
    assert again.headers["HX-Redirect"] == f"/artist/{CAROL}?artist_page=1"


def test_undo_puts_back_the_write_and_can_itself_be_undone(identify):
    client, path, key, _lookup = identify
    identify_as(client, key, key)
    _page, events, writes = history(client, CAROL)
    identified = writes[next(e.id for e in events if e.message.startswith("Identified"))]

    undone = client.post(f"/artist/{CAROL}/undo", data={"event_id": identified.event_id})
    assert undone.headers["HX-Redirect"] == f"/artist/{key}"
    assert "musicbrainzartistid" not in path.read_text()
    _page, events, writes = history(client, key)
    put_back = writes[next(e.id for e in events if e.message.startswith("NFO fields put back"))]
    assert put_back.undoable
    # The identification is no longer undoable: its fields are back already.
    assert not writes[next(e.id for e in events if e.message.startswith("Identified"))].undoable

    redone = client.post(f"/artist/{key}/undo", data={"event_id": put_back.event_id})
    assert redone.headers["HX-Redirect"] == f"/artist/{CAROL}"
    assert f"<musicbrainzartistid>{CAROL}</musicbrainzartistid>" in path.read_text()


def test_undo_leaves_a_field_changed_since_alone_and_says_so(identify):
    client, path, key, _lookup = identify
    identify_as(client, key, key)
    path.write_text(path.read_text().replace("<name>Carol</name>", "<name>Mine</name>"))
    _page, events, writes = history(client, CAROL)
    write = writes[next(e.id for e in events if e.message.startswith("Identified"))]
    response = client.post(f"/artist/{CAROL}/undo", data={"event_id": write.event_id})
    assert "left alone, changed since: Name" in response.headers["HX-Trigger"]
    text = path.read_text()
    assert "<name>Mine</name>" in text
    assert "<formed>" not in text and "musicbrainzartistid" not in text


def test_undoing_twice_changes_and_records_nothing_the_second_time(identify):
    client, _path, key, _lookup = identify
    identify_as(client, key, key)
    _page, events, writes = history(client, CAROL)
    write = writes[next(e.id for e in events if e.message.startswith("Identified"))]
    client.post(f"/artist/{CAROL}/undo", data={"event_id": write.event_id})
    recorded = activity_store.artist_history(key)
    again = client.post(f"/artist/{key}/undo", data={"event_id": write.event_id})
    assert "Nothing to undo" in again.headers["HX-Trigger"]
    assert activity_store.artist_history(key) == recorded


def test_undo_takes_only_a_write_from_this_pages_history(identify):
    client, _path, key, _lookup = identify
    identify_as(client, key, key)
    write = next(iter(history(client, CAROL)[2].values()))
    response = client.post(f"/artist/{ALICE}/undo", data={"event_id": write.event_id})
    assert response.status_code == 404


def test_a_request_names_only_a_file_on_the_page(identify):
    client, path, key, _lookup = identify
    assert look_up(client, ALICE, key).status_code == 404
    assert identify_as(client, ALICE, key).status_code == 404
    assert path.read_bytes() == b""


def test_an_unknown_artist_id_is_said_so_and_writes_nothing(identify, monkeypatch):
    client, path, key, _lookup = identify
    missing = Mock(side_effect=mb_lookup.ArtistNotFoundError("gone"))
    monkeypatch.setattr(mb_lookup, "fetch_artist", missing)
    assert "MusicBrainz has no artist with this ID." in look_up(client, key, key).text
    assert "MusicBrainz has no artist with this ID." in identify_as(client, key, key).text
    assert path.read_bytes() == b""


def test_musicbrainz_being_unreachable_writes_nothing(identify, monkeypatch):
    client, path, key, _lookup = identify
    monkeypatch.setattr(mb_lookup, "fetch_artist", Mock(side_effect=mb_lookup.MBError("503")))
    response = identify_as(client, key, key)
    assert "read MusicBrainz. Try again later." in response.text
    assert "HX-Redirect" not in response.headers
    assert path.read_bytes() == b""


def test_a_file_that_cannot_be_written_safely_says_why_and_records_it(identify):
    client, path, key, _lookup = identify
    original = "<artist><name>A</name><name>B</name></artist>"
    path.write_text(original)
    response = identify_as(client, key, key)
    assert "Nothing was written: the file has more than one &lt;name&gt;" in response.text
    assert "HX-Redirect" not in response.headers
    assert path.read_text() == original
    assert [e.message for e in activity_store.artist_history(key)] == [
        "Couldn't write the artist NFO — the file has more than one <name>"
    ]


def test_a_history_that_cannot_be_read_says_so(identify, monkeypatch):
    client, _path, key, _lookup = identify

    def broken(*_args, **_kwargs):
        raise activity_store.StoreUnavailableError("disk I/O error")

    monkeypatch.setattr(activity_store, "artist_history", broken)
    page = client.get(f"/artist/{key}")
    assert page.status_code == 200
    assert "History unavailable." in page.text


# A biography from Wikipedia, through MusicBrainz's link to Wikidata (#1018).

WIKIDATA = {
    "type": "wikidata",
    "target": "https://www.wikidata.org/wiki/Q123",
    "direction": "forward",
}
BIOGRAPHY = wikimedia.Biography(
    text="Carol are a band from Pawnee.\nThey formed in 1999.",
    language="en",
    url="https://en.wikipedia.org/wiki/Carol_(band)",
    revision="42",
)


@pytest.fixture
def wikipedia(identify, monkeypatch):
    """Carol, linked to a Wikidata item on MusicBrainz, and Wikipedia's article
    for it."""
    client, path, key, lookup = identify
    lookup.side_effect = lambda mbid: {**CAROL_MB, "url-relation-list": [dict(WIKIDATA)]}
    found = Mock(return_value=BIOGRAPHY)
    monkeypatch.setattr(wikimedia, "biography", found)
    return client, path, key, found


def flash(response):
    return json.loads(response.headers["HX-Trigger"])["harmonist-status"]


def biography_link(client, key):
    page = BeautifulSoup(client.get(f"/artist/{key}").text, "html.parser")
    return page.select_one("#artist-biography a")


def test_identify_fills_in_a_biography_from_the_linked_wikipedia_article(wikipedia):
    client, path, key, found = wikipedia
    response = identify_as(client, key, key)
    assert response.headers["HX-Redirect"] == f"/artist/{CAROL}"
    found.assert_called_once_with("Q123", languages=["en"], user_agent=mb_lookup_agent(client))
    written = path.read_text()
    assert (
        "<formed>1999</formed>\n    <biography>Carol are a band from Pawnee.\n"
        "They formed in 1999.</biography>\n</artist>"
    ) in written

    _page, events, writes = history(client, CAROL)
    write = writes[next(e.id for e in events if e.message.startswith("Identified"))]
    (change,) = [c for c in write.changes if c.field == "biography"]
    assert (change.before, change.after) == (None, BIOGRAPHY.text)
    assert change.credit == nfo_history.Credit(BIOGRAPHY.url, BIOGRAPHY.revision)
    link = biography_link(client, CAROL)
    assert link is not None and link["href"] == BIOGRAPHY.url
    assert link.get_text(" ", strip=True) == "From Wikipedia ↗"
    # Drawing the page asked nobody anything.
    assert found.call_count == 1


def mb_lookup_agent(client):
    return client.app.state.cfg.musicbrainz.user_agent


def test_identifying_twice_fills_the_biography_in_once(wikipedia):
    """The second time, the file has the biography, so Wikipedia isn't asked
    and nothing is written or recorded."""
    client, path, key, found = wikipedia
    identify_as(client, key, key)
    written, recorded = path.read_bytes(), activity_store.artist_history(CAROL)
    again = identify_as(client, CAROL, key)
    assert flash(again)["verb"] == "Nothing to change"
    assert path.read_bytes() == written
    assert activity_store.artist_history(CAROL) == recorded
    assert found.call_count == 1


def test_the_biography_comes_from_the_languages_asked_for_in_order(wikipedia):
    client, _path, key, found = wikipedia
    client.app.state.cfg.artists.biography_languages = ["pt", "en"]
    identify_as(client, key, key)
    assert found.call_args.kwargs["languages"] == ["pt", "en"]


def test_a_biography_already_there_is_kept_and_wikipedia_is_not_asked(wikipedia):
    client, path, key, found = wikipedia
    path.write_text("<artist>\n    <biography>Mine</biography>\n</artist>")
    identify_as(client, key, key)
    assert "<biography>Mine</biography>" in path.read_text()
    assert "Carol are a band" not in path.read_text()
    found.assert_not_called()


def test_a_biography_edited_since_is_no_longer_credited_to_wikipedia(wikipedia):
    client, path, key, _found = wikipedia
    identify_as(client, key, key)
    path.write_text(path.read_text().replace("from Pawnee", "from Eagleton"))
    assert biography_link(client, CAROL) is None


def test_undo_takes_away_the_biography_it_filled_in(wikipedia):
    client, path, key, _found = wikipedia
    identify_as(client, key, key)
    _page, events, writes = history(client, CAROL)
    write = writes[next(e.id for e in events if e.message.startswith("Identified"))]
    client.post(f"/artist/{CAROL}/undo", data={"event_id": write.event_id})
    assert "<biography>" not in path.read_text()


def test_identifying_again_fills_in_a_biography_that_is_missing(wikipedia):
    """How an artist identified before biographies came, or before MusicBrainz
    linked a Wikidata item, gets one."""
    client, path, key, found = wikipedia
    found.return_value = None
    identify_as(client, key, key)
    assert "<biography>" not in path.read_text()

    found.return_value = BIOGRAPHY
    again = identify_as(client, CAROL, key)
    assert flash(again)["verb"] == "Filled in"
    assert "<biography>Carol are a band" in path.read_text()


def test_wikipedia_unreachable_still_identifies_and_says_the_biography_is_missing(wikipedia):
    client, path, key, found = wikipedia
    found.side_effect = wikimedia.WikimediaError("503")
    response = identify_as(client, key, key)
    assert response.headers["HX-Redirect"] == f"/artist/{CAROL}"
    assert f"<musicbrainzartistid>{CAROL}</musicbrainzartistid>" in path.read_text()
    assert "<biography>" not in path.read_text()
    status = flash(response)
    assert status["level"] == Level.WARNING
    assert "couldn't fetch a biography from Wikipedia" in status["details"]
    (entry,) = [
        e
        for e in activity_store.artist_history(CAROL)
        if e.source is Source.ACTIVITY and e.message.startswith("Identified")
    ]
    assert entry.level is Level.WARNING


def test_wikipedia_unreachable_says_so_even_when_nothing_else_changes(wikipedia):
    client, _path, key, found = wikipedia
    found.return_value = None
    identify_as(client, key, key)
    found.return_value, found.side_effect = None, wikimedia.WikimediaError("503")
    again = identify_as(client, CAROL, key)
    status = flash(again)
    assert status["verb"] == "Nothing to change"
    assert status["level"] == Level.WARNING
    assert "couldn't fetch a biography from Wikipedia" in status["details"]


def test_no_article_identifies_and_says_there_is_none(wikipedia):
    client, path, key, found = wikipedia
    found.return_value = None
    response = identify_as(client, key, key)
    assert "<biography>" not in path.read_text()
    status = flash(response)
    assert status["level"] == Level.INFO
    assert "no Wikipedia article" in status["details"]


def test_no_wikidata_link_on_musicbrainz_asks_wikipedia_nothing(identify, monkeypatch):
    client, path, key, _lookup = identify
    found = Mock(side_effect=AssertionError("Wikipedia was asked"))
    monkeypatch.setattr(wikimedia, "biography", found)
    response = identify_as(client, key, key)
    assert "<biography>" not in path.read_text()
    assert "MusicBrainz links no Wikidata item" in flash(response)["details"]
