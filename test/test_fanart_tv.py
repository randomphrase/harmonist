"""An artist's picture from fanart.tv, by MusicBrainz ID (#1020)."""

from __future__ import annotations

import httpx
import pytest

from harmonist import fanart_tv

MBID = "77777777-7777-4777-8777-777777777777"
THUMB = f"https://assets.fanart.tv/fanart/music/{MBID}/artistthumb/carol-1.jpg"


def ask(handler, *, personal_key=""):
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    with httpx.Client(transport=httpx.MockTransport(record)) as client:
        found = fanart_tv.artist_thumb(
            MBID, personal_key=personal_key, user_agent="Harmonist/test", client=client
        )
    return found, requests


def answering(body, status=200):
    return lambda request: httpx.Response(status, json=body)


def test_the_first_thumbnail_is_the_picture_and_its_page_is_credited(monkeypatch):
    monkeypatch.setattr(fanart_tv, "PROJECT_KEY", "project")
    found, requests = ask(
        answering(
            {
                "name": "Carol",
                "artistthumb": [
                    {"id": "1", "url": THUMB, "likes": "3", "copyright": "Carol Records"},
                    {"id": "2", "url": THUMB.replace("-1", "-2"), "likes": "9"},
                ],
            }
        ),
        personal_key="mine",
    )
    assert found == fanart_tv.Thumb(
        url=THUMB, page=f"https://fanart.tv/artist/{MBID}/", copyright="Carol Records"
    )
    (request,) = requests
    assert str(request.url) == f"https://webservice.fanart.tv/v3.2/music/{MBID}"
    # Both keys go in headers, so neither is in an address anything logs.
    assert request.headers["api-key"] == "project"
    assert request.headers["client-key"] == "mine"
    assert request.headers["User-Agent"] == "Harmonist/test"


def test_an_unknown_copyright_holder_is_none():
    found, _ = ask(answering({"artistthumb": [{"url": THUMB, "copyright": "Unknown"}]}))
    assert found is not None and found.copyright == ""


def test_no_personal_key_sends_no_client_key(monkeypatch):
    monkeypatch.setattr(fanart_tv, "PROJECT_KEY", "project")
    _, (request,) = ask(answering({"artistthumb": [{"url": THUMB}]}))
    assert "client-key" not in request.headers


@pytest.mark.parametrize(
    "body", [{"name": "Carol"}, {"artistthumb": []}, {"hdmusiclogo": [{"url": THUMB}]}]
)
def test_an_artist_without_thumbnails_has_no_picture(body):
    assert ask(answering(body))[0] is None


def test_an_artist_fanart_tv_doesnt_have_is_no_picture_not_a_failure():
    assert ask(answering({"status": "error"}, status=404))[0] is None


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/carol.jpg",
        "http://assets.fanart.tv/fanart/music/carol.jpg",
        "javascript:alert(1)",
        "",
    ],
)
def test_a_thumbnail_anywhere_but_fanart_tvs_image_host_is_refused(url):
    with pytest.raises(fanart_tv.FanartError, match="isn't on assets.fanart.tv"):
        ask(answering({"artistthumb": [{"url": url}]}))


@pytest.mark.parametrize(
    "handler",
    [
        answering({"status": "error"}, status=500),
        answering({"status": "error"}, status=401),
        lambda request: httpx.Response(200, text="<html>"),
        answering(["not", "an", "object"]),
    ],
)
def test_an_answer_that_cant_be_used_is_a_failure_not_no_picture(handler):
    with pytest.raises(fanart_tv.FanartError):
        ask(handler)


def test_a_connection_failure_is_a_failure():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(fanart_tv.FanartError, match="refused"):
        ask(refuse)


def test_fanart_tv_is_asked_only_with_a_key(monkeypatch):
    monkeypatch.setattr(fanart_tv, "PROJECT_KEY", "")
    assert not fanart_tv.available("")
    assert fanart_tv.available("mine")
    monkeypatch.setattr(fanart_tv, "PROJECT_KEY", "project")
    assert fanart_tv.available("")
