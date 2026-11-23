"""What identifying an artist, or reading it again, fills into its NFO, and
why the Biography row says there is none (#1018, #1022)."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from harmonist import activity_store, artist_fill, nfo_history, wikimedia

MBID = "77777777-7777-4777-8777-777777777777"
LINKED = {
    "id": MBID,
    "name": "Carol",
    "url-relation-list": [{"type": "wikidata", "target": "https://www.wikidata.org/wiki/Q123"}],
}
UNLINKED = {"id": MBID, "name": "Carol"}
FOUND = wikimedia.Biography(
    text="Carol are a band.",
    language="en",
    url="https://en.wikipedia.org/wiki/Carol",
    revision="42",
)


@pytest.fixture(autouse=True)
def store(tmp_path):
    activity_store.init(tmp_path / "activity.db")


def look_up(answer, found, languages=("en",)):
    activity_store.store_artist(MBID, answer)
    return artist_fill.biography(MBID, answer, languages=list(languages), user_agent="test")


def note():
    return artist_fill.note(activity_store.cached_artist(MBID))


def test_a_biography_found_is_filled_and_credited(monkeypatch):
    monkeypatch.setattr(wikimedia, "biography", Mock(return_value=FOUND))
    filled = look_up(LINKED, FOUND)
    assert filled.biography == FOUND
    assert filled.credits == {"biography": nfo_history.Credit(FOUND.url, FOUND.revision)}
    assert not filled.failed
    assert note() is None


def test_no_wikidata_link_asks_wikipedia_nothing_and_says_so(monkeypatch):
    monkeypatch.setattr(wikimedia, "biography", Mock(side_effect=AssertionError("asked")))
    filled = look_up(UNLINKED, None)
    assert filled.biography is None and not filled.failed
    assert note() == "MusicBrainz links no Wikidata item"


def test_no_article_in_the_languages_asked_for_says_which(monkeypatch):
    monkeypatch.setattr(wikimedia, "biography", Mock(return_value=None))
    look_up(LINKED, None, languages=("pt", "en"))
    assert note() == "No Wikipedia article in pt or en"


def test_wikipedia_unreachable_is_a_failure_and_says_so(monkeypatch):
    monkeypatch.setattr(wikimedia, "biography", Mock(side_effect=wikimedia.WikimediaError("503")))
    filled = look_up(LINKED, None)
    assert filled.failed and filled.biography is None
    assert note() == "Wikipedia couldn't be reached"


def test_a_lookup_for_an_item_musicbrainz_no_longer_links_says_nothing(monkeypatch):
    """What the lookup found was about the old item, so it says nothing about
    the new one until the artist is read again and looked up."""
    monkeypatch.setattr(wikimedia, "biography", Mock(return_value=None))
    look_up(LINKED, None)
    relinked = {
        **LINKED,
        "url-relation-list": [{"type": "wikidata", "target": "https://www.wikidata.org/wiki/Q456"}],
    }
    activity_store.store_artist(MBID, relinked)
    assert note() is None


def test_an_artist_never_read_has_no_note():
    assert artist_fill.note(None) is None
