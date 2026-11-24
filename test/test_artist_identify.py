"""Filling in an artist NFO from MusicBrainz on its artist page, with History
and Undo (#981, #917)."""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from bs4 import BeautifulSoup

from harmonist import (
    activity_store,
    artist_fill,
    fanart_tv,
    id_registry,
    images,
    mb_lookup,
    mb_search,
    nfo_history,
    nfo_writer,
    wikimedia,
)
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
    text="Carol are a band from Pawnee & Eagleton.\nThey formed in 1999.",
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
    """The credit beside the Biography label, on the same line (#1025)."""
    page = BeautifulSoup(client.get(f"/artist/{key}").text, "html.parser")
    label = page.select_one("#artist-biography dt")
    assert label is not None and label.get_text(strip=True) == "Biography"
    links = label.find_next_sibling("dd").find_all("a")
    return next((a for a in links if "From Wikipedia" in a.get_text()), None)


def test_identify_fills_in_a_biography_from_the_linked_wikipedia_article(wikipedia):
    client, path, key, found = wikipedia
    response = identify_as(client, key, key)
    assert response.headers["HX-Redirect"] == f"/artist/{CAROL}"
    found.assert_called_once_with("Q123", languages=["en"], user_agent=mb_lookup_agent(client))
    written = path.read_text()
    # HTML, escaped as XML: players show a biography as HTML, and Navidrome
    # turns a newline into a space, so a line break keeps the paragraphs apart.
    assert (
        "<formed>1999</formed>\n    <biography>Carol are a band from Pawnee &amp;amp; Eagleton."
        "&lt;br&gt;&lt;br&gt;They formed in 1999.</biography>\n</artist>"
    ) in written

    page, events, writes = history(client, CAROL)
    write = writes[next(e.id for e in events if e.message.startswith("Identified"))]
    (change,) = [c for c in write.changes if c.field == "biography"]
    html = "Carol are a band from Pawnee &amp; Eagleton.<br><br>They formed in 1999."
    assert (change.before, change.after) == (None, html)
    assert change.credit == nfo_history.Credit(BIOGRAPHY.url, BIOGRAPHY.revision)
    # Harmonist reads it as HTML too: paragraphs, not markup, in Artist info
    # and in History.
    shown = "Carol are a band from Pawnee & Eagleton.\n\nThey formed in 1999."
    soup = BeautifulSoup(page.text, "html.parser")
    text = soup.select_one("#artist-biography .artist-bio__text")
    assert text is not None and text.get_text() == shown
    row = next(dt for dt in soup.select(".tag-changes dt") if dt.get_text() == "Biography")
    after = row.find_next_sibling("dd").select_one(".tag-changes__after")
    assert after is not None and after.get_text() == shown
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


def biography_note(client, key):
    """What the Biography row says beside its label, where the file has no
    biography (#1022, #1025)."""
    page = BeautifulSoup(client.get(f"/artist/{key}").text, "html.parser")
    row = page.select_one("#artist-biography")
    assert row is not None, "an identified NFO always has a Biography row"
    assert row.select_one(".artist-bio__text") is None, "there is no biography"
    label = one(row, "dt")
    assert label.get_text(strip=True) == "Biography"
    return one(label.find_next_sibling("dd"), ".artist-info__note").get_text(" ", strip=True)


def add_link(client, key):
    """The button that opens the artist's edit page on MusicBrainz (#1025)."""
    page = BeautifulSoup(client.get(f"/artist/{key}").text, "html.parser")
    return page.select_one(f'#artist-biography a[href="https://musicbrainz.org/artist/{key}/edit"]')


def test_no_wikidata_link_offers_to_add_one_on_musicbrainz(identify):
    client, _path, key, _lookup = identify
    identify_as(client, key, key)
    button = add_link(client, CAROL)
    assert button is not None
    assert button.get_text(" ", strip=True) == "Add link on MB ↗"
    assert button["aria-label"] == "Add a Wikidata link on MusicBrainz"
    assert (button["target"], button["rel"]) == ("_blank", ["noopener"])


def test_a_linked_artist_without_an_article_offers_no_edit(wikipedia):
    """Editing the artist wouldn't help: MusicBrainz's link is there."""
    client, _path, key, found = wikipedia
    found.return_value = None
    identify_as(client, key, key)
    assert biography_note(client, CAROL) == "No Wikipedia article in en"
    assert add_link(client, CAROL) is None


def nfo_with_biography(client, text):
    """Alice's NFO, holding `text` as its biography."""
    path = next(client.app.state.cfg.paths.music_dir.rglob("Different directory name/artist.nfo"))
    path.write_text(
        f"<artist><name>Alice</name><musicbrainzartistid>{ALICE}</musicbrainzartistid>"
        f"<biography>{text}</biography></artist>"
    )
    page = BeautifulSoup(client.get(f"/artist/{ALICE}").text, "html.parser")
    return one(page, "#artist-biography")


def test_a_long_biography_shows_its_first_lines_and_show_all(identify):
    client, _path, _key, _lookup = identify
    row = nfo_with_biography(client, "A sentence about Alice. " * 40)
    text = one(row, ".artist-bio__text")
    toggle = one(row, "details.artist-bio__more")
    # The toggle follows the text, which the stylesheet clamps while it's closed,
    # and keeps its state through the page's redraws as any disclosure does.
    assert text.find_next_sibling("details") is toggle
    assert "hmt-group" in toggle["class"] and toggle["id"]
    assert toggle.summary.get_text(" ", strip=True) == "Show all Show less"
    assert len(toggle.contents) == len(toggle.find_all("summary", recursive=False)) + sum(
        1 for c in toggle.contents if isinstance(c, str)
    ), "the toggle holds nothing but its summary"


def test_a_short_biography_is_shown_whole(identify):
    client, _path, _key, _lookup = identify
    row = nfo_with_biography(client, "Alice sings.")
    assert one(row, ".artist-bio__text").get_text(strip=True) == "Alice sings."
    assert row.select_one("details") is None


def test_a_biography_of_several_short_paragraphs_counts_as_long(identify):
    client, _path, _key, _lookup = identify
    row = nfo_with_biography(client, "One.\nTwo.\nThree.\nFour.")
    assert row.select_one("details.artist-bio__more") is not None


def test_no_article_says_so_on_the_biography_row_not_in_the_answer(wikipedia):
    client, path, key, found = wikipedia
    found.return_value = None
    response = identify_as(client, key, key)
    assert "<biography>" not in path.read_text()
    status = flash(response)
    assert (status["level"], status["details"]) == (Level.INFO, "Carol/artist.nfo")
    assert biography_note(client, CAROL) == "No Wikipedia article in en"


def test_no_wikidata_link_on_musicbrainz_asks_wikipedia_nothing(identify, monkeypatch):
    client, path, key, _lookup = identify
    found = Mock(side_effect=AssertionError("Wikipedia was asked"))
    monkeypatch.setattr(wikimedia, "biography", found)
    response = identify_as(client, key, key)
    assert "<biography>" not in path.read_text()
    assert flash(response)["details"] == "Carol/artist.nfo"
    assert biography_note(client, CAROL) == "MusicBrainz links no Wikidata item"


def test_wikipedia_unreachable_says_so_on_the_biography_row_too(wikipedia):
    client, _path, key, found = wikipedia
    found.side_effect = wikimedia.WikimediaError("503")
    identify_as(client, key, key)
    assert biography_note(client, CAROL) == "Wikipedia couldn't be reached"


# When MusicBrainz was last read, and reading it again (#1022).


def checked(client, key):
    page = BeautifulSoup(client.get(f"/artist/{key}").text, "html.parser")
    return one(page, "#artist-checked")


def refresh(client, key, file_key, current=None):
    headers = {"HX-Current-URL": f"http://testserver{current}"} if current else {}
    return client.post(f"/artist/{key}/refresh", data={"file": file_key}, headers=headers)


def test_artist_info_says_when_musicbrainz_was_last_read(identify):
    client, _path, key, _lookup = identify
    # Alice's NFO was identified before anything was stored.
    assert "not yet" in checked(client, ALICE).get_text(" ", strip=True)
    identify_as(client, key, key)
    when = checked(client, CAROL).select_one("dd[title]")
    assert when is not None and when["title"].startswith("MusicBrainz artist last read")


def test_the_refresh_button_names_the_file_on_the_page(identify):
    client, _path, key, _lookup = identify
    identify_as(client, key, key)
    button = one(checked(client, CAROL), f'button[hx-post="/artist/{CAROL}/refresh"]')
    assert json.loads(button["hx-vals"]) == {"file": key}


def test_reading_again_fills_in_a_biography_musicbrainz_now_links(identify, monkeypatch):
    """The user adds the Wikidata link on MusicBrainz, then presses ↻."""
    client, path, key, lookup = identify
    identify_as(client, key, key)
    assert biography_note(client, CAROL) == "MusicBrainz links no Wikidata item"
    lookup.side_effect = lambda mbid: {**CAROL_MB, "url-relation-list": [dict(WIKIDATA)]}
    monkeypatch.setattr(wikimedia, "biography", Mock(return_value=BIOGRAPHY))

    response = refresh(client, CAROL, key, f"/artist/{CAROL}?artist_page=1")
    assert lookup.call_count == 2
    assert flash(response)["verb"] == "Filled in"
    assert response.headers["HX-Redirect"] == f"/artist/{CAROL}?artist_page=1"
    # As Identify writes it: HTML, escaped as XML.
    assert (
        "<biography>Carol are a band from Pawnee &amp;amp; Eagleton."
        "&lt;br&gt;&lt;br&gt;They formed in 1999.</biography>"
    ) in path.read_text()
    _page, events, writes = history(client, CAROL)
    write = writes[next(e.id for e in events if e.message.startswith("Filled in"))]
    assert [c.field for c in write.changes] == ["biography"]
    assert biography_link(client, CAROL)["href"] == BIOGRAPHY.url

    # Again: the biography is there now, so nothing is written or recorded.
    filled, recorded = path.read_bytes(), activity_store.artist_history(CAROL)
    refresh(client, CAROL, key)
    assert path.read_bytes() == filled
    assert activity_store.artist_history(CAROL) == recorded


def test_reading_again_writes_nothing_else_musicbrainz_changed(identify):
    """A changed name is step 2's update, with its own review; reading again
    only fills in what's missing."""
    client, path, key, lookup = identify
    identify_as(client, key, key)
    written = path.read_bytes()
    lookup.side_effect = lambda mbid: {**CAROL_MB, "name": "Carol (renamed)"}
    before = activity_store.artist_history(CAROL)

    response = refresh(client, CAROL, key)
    # Nothing to say: the page it redraws shows the new date.
    assert "harmonist-status" not in response.headers.get("HX-Trigger", "")
    assert response.headers["HX-Redirect"] == f"/artist/{CAROL}"
    assert path.read_bytes() == written
    assert activity_store.artist_history(CAROL) == before
    stored = activity_store.cached_artist(CAROL)
    assert stored is not None and stored.payload["name"] == "Carol (renamed)"


def test_reading_again_with_wikipedia_unreachable_warns(identify, monkeypatch):
    client, _path, key, lookup = identify
    identify_as(client, key, key)
    lookup.side_effect = lambda mbid: {**CAROL_MB, "url-relation-list": [dict(WIKIDATA)]}
    monkeypatch.setattr(wikimedia, "biography", Mock(side_effect=wikimedia.WikimediaError("x")))
    status = flash(refresh(client, CAROL, key))
    assert status["level"] == Level.WARNING
    assert "couldn't fetch a biography from Wikipedia" in status["details"]
    assert biography_note(client, CAROL) == "Wikipedia couldn't be reached"


def test_reading_again_with_musicbrainz_unreachable_writes_and_stores_nothing(identify):
    client, path, key, lookup = identify
    identify_as(client, key, key)
    written, stored = path.read_bytes(), activity_store.cached_artist(CAROL)
    lookup.side_effect = mb_lookup.MBError("503")
    status = flash(refresh(client, CAROL, key))
    assert status["level"] == Level.WARNING
    assert "read MusicBrainz" in status["details"]
    assert path.read_bytes() == written
    assert activity_store.cached_artist(CAROL) == stored


def test_only_an_identified_artist_is_read_again(identify):
    client, _path, key, lookup = identify
    assert refresh(client, key, key).status_code == 404
    lookup.assert_not_called()


# The artist's picture, filled in beside the NFO where its folder has none (#1020).

JPEG = b"\xff\xd8\xff\xe0" + b"jpeg" * 8
THUMB = fanart_tv.Thumb(
    url=f"https://assets.fanart.tv/fanart/music/{CAROL}/artistthumb/carol.jpg",
    page=f"https://fanart.tv/artist/{CAROL}/",
    copyright="Carol Records",
)
ON_COMMONS = wikimedia.CommonsPicture(
    url="https://upload.wikimedia.org/wikipedia/commons/thumb/c/ca/Carol.jpg/1000px-Carol.jpg",
    page="https://commons.wikimedia.org/wiki/File:Carol.jpg",
    author="Jo Bloggs",
    licence="CC BY-SA 4.0",
    licence_url="https://creativecommons.org/licenses/by-sa/4.0",
)


@pytest.fixture
def fanart(identify, monkeypatch):
    """fanart.tv with a key and a picture of Carol, which fetches as a JPEG."""
    client, path, key, _lookup = identify
    monkeypatch.setattr(fanart_tv, "PROJECT_KEY", "project")
    thumb = Mock(return_value=THUMB)
    monkeypatch.setattr(fanart_tv, "artist_thumb", thumb)
    monkeypatch.setattr(artist_fill, "download_picture", Mock(return_value=JPEG))
    return client, path, key, thumb


def picture_credit(client, key):
    page = BeautifulSoup(client.get(f"/artist/{key}").text, "html.parser")
    return page.select_one("#artist-picture-credit")


def picture_note(client, key):
    page = BeautifulSoup(client.get(f"/artist/{key}").text, "html.parser")
    note = page.select_one("#artist-picture-note")
    return note.get_text(" ", strip=True) if note is not None else None


def test_identify_puts_fanart_tvs_picture_beside_the_nfo_and_credits_it(fanart):
    client, path, key, thumb = fanart
    response = identify_as(client, key, key)
    picture = path.parent / "artist.jpg"
    assert picture.read_bytes() == JPEG
    thumb.assert_called_once_with(CAROL, personal_key="", user_agent=mb_lookup_agent(client))
    status = flash(response)
    assert (status["verb"], status["level"]) == ("Identified", Level.INFO)
    assert status["details"] == "Carol/artist.nfo; added artist.jpg"

    _page, events, writes = history(client, CAROL)
    write = writes[next(e.id for e in events if e.message.startswith("Identified"))]
    credit = nfo_history.PictureCredit("fanart.tv", THUMB.page, author="Carol Records")
    assert write.picture == nfo_history.PictureChange(
        "artist.jpg", images.digest(JPEG), True, credit
    )
    # The page shows the picture, and credits fanart.tv beneath it.
    caption = picture_credit(client, CAROL)
    assert caption is not None
    link = caption.find("a")
    assert link["href"] == THUMB.page
    assert caption.get_text(" ", strip=True) == "From fanart.tv ↗ · Carol Records"
    assert picture_note(client, CAROL) is None


def test_a_commons_picture_is_credited_with_its_author_and_licence(wikipedia, monkeypatch):
    client, path, key, _found = wikipedia
    monkeypatch.setattr(wikimedia, "picture", Mock(return_value=ON_COMMONS))
    monkeypatch.setattr(artist_fill, "download_picture", Mock(return_value=JPEG))
    identify_as(client, key, key)
    assert (path.parent / "artist.jpg").read_bytes() == JPEG
    caption = picture_credit(client, CAROL)
    assert caption is not None
    assert caption.get_text(" ", strip=True) == (
        "From Wikimedia Commons ↗ · Jo Bloggs · CC BY-SA 4.0"
    )
    assert [a["href"] for a in caption.find_all("a")] == [ON_COMMONS.page, ON_COMMONS.licence_url]


def test_a_folder_with_a_picture_asks_for_none_and_says_why(fanart):
    """Any picture a player reads counts, whether or not Harmonist shows it."""
    client, path, key, thumb = fanart
    (path.parent / "Cover.JPG").write_bytes(JPEG)
    identify_as(client, key, key)
    thumb.assert_not_called()
    assert not (path.parent / "artist.jpg").exists()
    assert picture_note(client, CAROL) == "None fetched, since Cover.JPG is here"


def test_a_picture_that_cant_be_fetched_doesnt_stop_the_identification(fanart):
    client, path, key, thumb = fanart
    thumb.side_effect = fanart_tv.FanartError("503")
    response = identify_as(client, key, key)
    assert "<name>Carol</name>" in path.read_text()
    assert not (path.parent / "artist.jpg").exists()
    status = flash(response)
    assert (status["verb"], status["level"]) == ("Identified", Level.WARNING)
    assert status["details"] == "Carol/artist.nfo; couldn't fetch a picture from fanart.tv"
    assert picture_note(client, CAROL) == "fanart.tv couldn't be reached"


def test_undo_takes_the_picture_away_with_the_identification(fanart):
    """A wrong identification's picture is of the wrong artist."""
    client, path, key, _thumb = fanart
    identify_as(client, key, key)
    _page, events, writes = history(client, CAROL)
    write = writes[next(e.id for e in events if e.message.startswith("Identified"))]
    assert write.undoable

    response = client.post(f"/artist/{CAROL}/undo", data={"event_id": write.event_id})
    assert flash(response)["details"] == "Carol/artist.nfo; removed artist.jpg"
    assert not (path.parent / "artist.jpg").exists()
    assert "<musicbrainzartistid>" not in path.read_text()
    _page, events, writes = history(client, key)
    undo = writes[next(e.id for e in events if e.message.startswith("NFO fields put back"))]
    assert undo.picture == nfo_history.PictureChange("artist.jpg", images.digest(JPEG), False)


def test_undo_leaves_a_picture_replaced_since(fanart):
    client, path, key, _thumb = fanart
    identify_as(client, key, key)
    picture = path.parent / "artist.jpg"
    picture.write_bytes(JPEG + b"edited")
    # No longer the picture fanart.tv gave, so no longer credited to it.
    assert picture_credit(client, CAROL) is None
    _page, events, writes = history(client, CAROL)
    write = writes[next(e.id for e in events if e.message.startswith("Identified"))]

    response = client.post(f"/artist/{CAROL}/undo", data={"event_id": write.event_id})
    assert flash(response)["details"] == "left alone, changed since: artist.jpg"
    assert picture.read_bytes() == JPEG + b"edited"
    assert "<musicbrainzartistid>" not in path.read_text()


def test_undoing_a_write_that_only_added_a_picture_removes_it(fanart, monkeypatch):
    client, path, key, thumb = fanart
    thumb.return_value = None
    identify_as(client, key, key)
    thumb.return_value = THUMB
    response = refresh(client, CAROL, key)
    assert flash(response)["details"] == "Carol/artist.nfo; added artist.jpg"
    _page, events, writes = history(client, CAROL)
    write = writes[next(e.id for e in events if e.message.startswith("Filled in"))]
    assert write.changes == () and write.picture is not None

    response = client.post(f"/artist/{CAROL}/undo", data={"event_id": write.event_id})
    assert flash(response)["verb"] == "Removed artist.jpg"
    assert not (path.parent / "artist.jpg").exists()
    assert "<name>Carol</name>" in path.read_text()


def test_reading_again_fills_in_a_missing_picture_once(fanart):
    """Artists identified before pictures were looked up get one this way."""
    client, path, key, thumb = fanart
    thumb.return_value = None
    identify_as(client, key, key)
    assert picture_note(client, CAROL) == (
        "None on fanart.tv, and MusicBrainz links no Wikidata item"
    )
    thumb.return_value = THUMB
    response = refresh(client, CAROL, key)
    assert flash(response)["verb"] == "Filled in"
    assert (path.parent / "artist.jpg").read_bytes() == JPEG

    # Again: the folder has its picture, so nobody is asked and nothing written.
    thumb.reset_mock()
    recorded = activity_store.artist_history(CAROL)
    refresh(client, CAROL, key)
    thumb.assert_not_called()
    assert activity_store.artist_history(CAROL) == recorded


def test_a_picture_that_cant_be_written_is_said_beside_what_was(fanart, monkeypatch):
    client, path, key, _thumb = fanart
    temporary_for = nfo_writer.temporary_for

    def nowhere(target):
        """The picture's way in leads nowhere; the NFO's still works."""
        return (
            target.parent / "gone" / target.name
            if target.suffix == ".jpg"
            else temporary_for(target)
        )

    monkeypatch.setattr(nfo_writer, "temporary_for", nowhere)
    response = identify_as(client, key, key)
    assert "<name>Carol</name>" in path.read_text()
    assert not (path.parent / "artist.jpg").exists()
    status = flash(response)
    assert status["level"] == Level.WARNING
    assert status["details"] == (
        "Carol/artist.nfo; couldn't write artist.jpg: No such file or directory"
    )


def test_a_store_that_cannot_be_read_says_so_rather_than_not_yet(identify, monkeypatch):
    client, _path, _key, _lookup = identify

    def broken(_mbid):
        raise activity_store.StoreUnavailableError("disk I/O error")

    monkeypatch.setattr(activity_store, "cached_artist", broken)
    response = client.get(f"/artist/{ALICE}")
    assert response.status_code == 200
    text = checked(client, ALICE).get_text(" ", strip=True)
    assert "unavailable" in text and "not yet" not in text
