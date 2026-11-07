"""Filling in an artist NFO from MusicBrainz on its artist page, with History
and Undo (#981, #917)."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from bs4 import BeautifulSoup

from harmonist import activity_store, id_registry, mb_lookup, mb_search, nfo_history
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
    assert page.select_one(f"details#artist-identify-{key}") is None


def test_an_identified_nfo_folds_its_finder_under_identify_again(identify):
    """Its own ID is in the MBID field, ready to fill in what the file lacks."""
    client, _path, _key, _lookup = identify
    page = BeautifulSoup(client.get(f"/artist/{ALICE}").text, "html.parser")
    folded = page.select_one("details[id^=artist-identify-]")
    assert folded is not None
    checked = folded.select_one('input[type="radio"][checked]')
    assert checked is not None and checked["value"] == "mbid"
    pasted = folded.select_one('input[name="artist"]')
    assert pasted is not None and pasted["value"] == ALICE


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
    assert "HX-Redirect" not in again.headers
    assert activity_store.artist_history(CAROL) == before


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
