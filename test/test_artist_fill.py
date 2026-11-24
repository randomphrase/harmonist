"""What identifying an artist, or reading it again, fills into its NFO, and
why the Biography row says there is none (#1018, #1022), and the picture
filled in beside it (#1020)."""

from __future__ import annotations

import threading
from dataclasses import replace
from unittest.mock import Mock

import httpx
import pytest

from harmonist import (
    activity_store,
    artist_fill,
    artist_nfo,
    fanart_tv,
    mb_lookup,
    nfo_history,
    wikimedia,
)

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


# The picture (#1020): fanart.tv's, or failing that the one Wikidata gives
# the item, looked up beside MusicBrainz and the biography.

MERGED = "88888888-8888-4888-8888-888888888888"
ASKING = artist_fill.Asking(languages=["en"], user_agent="test")
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg" * 8
PNG = b"\x89PNG\r\n\x1a\n" + b"png" * 8
THUMB = fanart_tv.Thumb(
    url=f"https://assets.fanart.tv/fanart/music/{MBID}/artistthumb/carol.jpg",
    page=f"https://fanart.tv/artist/{MBID}/",
)
ON_COMMONS = wikimedia.CommonsPicture(
    url="https://upload.wikimedia.org/wikipedia/commons/thumb/c/ca/Carol.png/1000px-Carol.png",
    page="https://commons.wikimedia.org/wiki/File:Carol.png",
    author="Jo Bloggs",
    licence="CC BY 4.0",
    licence_url="https://creativecommons.org/licenses/by/4.0",
)


@pytest.fixture
def pictures(monkeypatch):
    """fanart.tv with a key, and neither it nor Commons having a picture
    until a test says so. Fetching a picture gives the JPEG."""
    monkeypatch.setattr(fanart_tv, "PROJECT_KEY", "project")
    thumb = Mock(return_value=None)
    on_commons = Mock(return_value=None)
    fetch = Mock(return_value=JPEG)
    monkeypatch.setattr(fanart_tv, "artist_thumb", thumb)
    monkeypatch.setattr(wikimedia, "picture", on_commons)
    monkeypatch.setattr(artist_fill, "download_picture", fetch)
    return thumb, on_commons, fetch


def reading(answer):
    """MusicBrainz answering with `answer`, which is kept, as it is in use."""

    def read():
        activity_store.store_artist(str(answer["id"]), answer)
        return answer

    return read


def fill(read, *, lacks_biography=False, lacks_picture=True):
    return artist_fill.fill(
        MBID, read, lacks_biography=lacks_biography, lacks_picture=lacks_picture, asking=ASKING
    )


def picture_note(mbid=MBID):
    return artist_fill.picture_note(activity_store.cached_artist(mbid))


def test_fanart_tvs_picture_is_filled_in_and_credited(pictures):
    thumb, on_commons, fetch = pictures
    thumb.return_value = replace(THUMB, copyright="Carol Records")
    _, filled = fill(reading(LINKED))
    assert filled.picture == nfo_history.NewPicture(
        JPEG, nfo_history.PictureCredit("fanart.tv", THUMB.page, author="Carol Records")
    )
    assert filled.picture.name == "artist.jpg"
    assert filled.picture_failed is None
    thumb.assert_called_once_with(MBID, personal_key="", user_agent="test")
    fetch.assert_called_once_with(THUMB.url, user_agent="test")
    on_commons.assert_not_called()


def test_fanart_tv_is_asked_while_musicbrainz_is(pictures):
    """fanart.tv needs only the ID, so MusicBrainz's answer isn't waited for.
    Asked one after the other, MusicBrainz would wait out its deadline here."""
    thumb, _on_commons, _fetch = pictures
    asked = threading.Event()
    thumb.side_effect = lambda *args, **kwargs: asked.set()

    def read():
        assert asked.wait(timeout=5), "fanart.tv wasn't asked while MusicBrainz was"
        return reading(LINKED)()

    fill(read)
    assert asked.is_set()


def test_the_lookups_belong_to_the_action_that_asked_for_them(pictures):
    """A thread doesn't inherit the action an identification runs in, and
    what the lookups record has to be part of it."""
    thumb, on_commons, _fetch = pictures
    seen = []
    thumb.side_effect = lambda *args, **kwargs: seen.append(activity_store.current_action())
    with activity_store.action() as action:
        fill(reading(LINKED))
    assert seen == [action]
    on_commons.assert_called_once()


def test_a_merged_artist_is_looked_up_by_the_id_musicbrainz_answers_with(pictures):
    thumb, _on_commons, _fetch = pictures
    thumb.side_effect = lambda mbid, **kwargs: THUMB if mbid == MERGED else None
    answer, filled = fill(reading({**LINKED, "id": MERGED}))
    assert answer["id"] == MERGED
    assert filled.picture is not None
    assert thumb.call_args_list[-1].args == (MERGED,)
    assert activity_store.cached_artist(MERGED).picture == {
        "status": "found",
        "service": "fanart.tv",
    }


def test_a_failure_at_fanart_tv_takes_nothing_from_commons_instead(pictures):
    """A picture is filled in only where there is none, so Commons' picture
    in fanart.tv's place would stand for good."""
    thumb, on_commons, _fetch = pictures
    thumb.side_effect = fanart_tv.FanartError("503")
    _, filled = fill(reading(LINKED))
    assert filled.picture is None and filled.picture_failed == "fanart.tv"
    on_commons.assert_not_called()
    assert picture_note() == "fanart.tv couldn't be reached"


def test_a_picture_fanart_tv_names_but_cant_give_is_a_failure(pictures):
    thumb, on_commons, fetch = pictures
    thumb.return_value = THUMB
    fetch.side_effect = artist_fill.PictureFetchError("timed out")
    _, filled = fill(reading(LINKED))
    assert filled.picture is None and filled.picture_failed == "fanart.tv"
    on_commons.assert_not_called()


def test_without_a_picture_on_fanart_tv_the_items_image_is_taken_and_credited(pictures):
    _thumb, on_commons, fetch = pictures
    on_commons.return_value = ON_COMMONS
    fetch.return_value = PNG
    _, filled = fill(reading(LINKED))
    assert filled.picture == nfo_history.NewPicture(
        PNG,
        nfo_history.PictureCredit(
            "Wikimedia Commons",
            ON_COMMONS.page,
            author="Jo Bloggs",
            licence="CC BY 4.0",
            licence_url="https://creativecommons.org/licenses/by/4.0",
        ),
    )
    assert filled.picture.name == "artist.png"
    on_commons.assert_called_once_with("Q123", user_agent="test")


def test_a_picture_that_isnt_a_jpeg_or_png_on_fanart_tv_leaves_it_to_commons(pictures):
    thumb, on_commons, fetch = pictures
    thumb.return_value = THUMB
    on_commons.return_value = ON_COMMONS
    fetch.side_effect = [None, PNG]
    _, filled = fill(reading(LINKED))
    assert filled.picture is not None and filled.picture.credit.source == "Wikimedia Commons"


def test_no_picture_anywhere_says_where_it_was_looked_for(pictures):
    fill(reading(LINKED))
    assert picture_note() == "None on fanart.tv or Wikimedia Commons"


def test_no_wikidata_link_asks_commons_nothing_and_says_so(pictures):
    _thumb, on_commons, _fetch = pictures
    _, filled = fill(reading(UNLINKED))
    assert filled.picture is None and filled.picture_failed is None
    on_commons.assert_not_called()
    assert picture_note() == "None on fanart.tv, and MusicBrainz links no Wikidata item"


def test_commons_unreachable_is_a_failure_and_says_so(pictures):
    _thumb, on_commons, _fetch = pictures
    on_commons.side_effect = wikimedia.WikimediaError("503")
    _, filled = fill(reading(LINKED))
    assert filled.picture_failed == "Wikimedia Commons"
    assert picture_note() == "Wikimedia Commons couldn't be reached"


def test_without_a_key_fanart_tv_isnt_asked(pictures, monkeypatch):
    thumb, on_commons, _fetch = pictures
    monkeypatch.setattr(fanart_tv, "PROJECT_KEY", "")
    fill(reading(LINKED))
    thumb.assert_not_called()
    on_commons.assert_called_once()


def test_a_folder_with_a_picture_asks_nobody_for_one(pictures):
    thumb, on_commons, _fetch = pictures
    _, filled = fill(reading(LINKED), lacks_picture=False)
    assert filled.picture is None
    thumb.assert_not_called()
    on_commons.assert_not_called()


def test_the_biography_and_the_picture_are_filled_in_together(pictures, monkeypatch):
    thumb, _on_commons, _fetch = pictures
    thumb.return_value = THUMB
    monkeypatch.setattr(wikimedia, "biography", Mock(return_value=FOUND))
    _, filled = fill(reading(LINKED), lacks_biography=True)
    assert filled.biography == FOUND and filled.picture is not None


def test_when_musicbrainz_cant_be_read_nothing_else_is_waited_for(pictures):
    """The user is told at once, rather than when fanart.tv answers about an
    artist nothing will be written for."""
    thumb, _on_commons, _fetch = pictures
    release, finished = threading.Event(), threading.Event()

    def slow(*args, **kwargs):
        release.wait(timeout=5)
        finished.set()

    thumb.side_effect = slow

    def read():
        raise mb_lookup.MBError("down")

    try:
        with pytest.raises(mb_lookup.MBError):
            fill(read)
        assert not finished.is_set()
    finally:
        release.set()


def test_no_picture_lookup_has_no_note():
    activity_store.store_artist(MBID, LINKED)
    assert picture_note() is None
    assert artist_fill.picture_note(None) is None


def fetched(handler, monkeypatch=None):
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return artist_fill.download_picture(
            "https://assets.fanart.tv/x.jpg", user_agent="test", client=client
        )


@pytest.mark.parametrize(("data", "expected"), [(JPEG, JPEG), (PNG, PNG), (b"GIF89a....", None)])
def test_only_a_jpeg_or_a_png_is_fetched_to_write(data, expected):
    assert fetched(lambda request: httpx.Response(200, content=data)) == expected


def test_a_picture_over_the_limit_is_a_failure(monkeypatch):
    monkeypatch.setattr(artist_nfo, "IMAGE_LIMIT", len(JPEG) - 1)
    with pytest.raises(artist_fill.PictureFetchError, match="is over"):
        fetched(lambda request: httpx.Response(200, content=JPEG))


@pytest.mark.parametrize("status", [302, 404, 503])
def test_a_redirect_or_an_error_is_a_failure(status):
    """A redirect isn't followed: the address was checked, and where it leads
    wasn't."""
    with pytest.raises(artist_fill.PictureFetchError):
        fetched(
            lambda request: httpx.Response(status, headers={"Location": "https://example.com/"})
        )
