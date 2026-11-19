"""An artist's biography from Wikipedia, found through MusicBrainz's link to
the artist's Wikidata item and never by name (#1018)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from harmonist import wikimedia

AGENT = "Harmonist/1.x ( test@example.com )"
ITEM = "Q44190"
INTRO = "Radiohead are an English rock band formed in Abingdon, Oxfordshire, in 1985.\nThey have sold more than 30 million albums."


def wikidata(url: str, *, ended: bool = False) -> dict[str, Any]:
    relation: dict[str, Any] = {"type": "wikidata", "target": url, "direction": "forward"}
    if ended:
        relation["ended"] = "true"
    return relation


def artist(*relations: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "a74b1b7f-71a5-4011-9441-d0b5e4122711",
        "name": "Radiohead",
        "url-relation-list": list(relations),
    }


def test_the_item_is_the_one_current_wikidata_link():
    other = {"type": "official homepage", "target": "https://www.radiohead.com/"}
    assert (
        wikimedia.wikidata_item(artist(other, wikidata(f"https://www.wikidata.org/wiki/{ITEM}")))
        == ITEM
    )


def test_an_ended_link_is_not_the_artists_item():
    linked = artist(
        wikidata("https://www.wikidata.org/wiki/Q1", ended=True),
        wikidata(f"https://www.wikidata.org/wiki/{ITEM}"),
    )
    assert wikimedia.wikidata_item(linked) == ITEM
    assert (
        wikimedia.wikidata_item(artist(wikidata("https://www.wikidata.org/wiki/Q1", ended=True)))
        is None
    )


def test_two_current_links_name_no_item_rather_than_a_guess():
    assert (
        wikimedia.wikidata_item(
            artist(
                wikidata("https://www.wikidata.org/wiki/Q1"),
                wikidata("https://www.wikidata.org/wiki/Q2"),
            )
        )
        is None
    )


def test_no_link_or_a_link_to_something_else_names_no_item():
    assert wikimedia.wikidata_item({"id": "x", "name": "Nobody"}) is None
    assert (
        wikimedia.wikidata_item(artist(wikidata("https://www.wikidata.org/wiki/Property:P434")))
        is None
    )


class Wikimedia:
    """Wikidata and the Wikipedias, answering from what a test gives them, and
    the requests they were sent."""

    def __init__(self, sitelinks: dict[str, str], *, extract: str = INTRO, missing: bool = False):
        self.sitelinks = sitelinks
        self.extract = extract
        self.missing = missing
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        params = request.url.params
        if request.url.host == "www.wikidata.org":
            wanted = params["sitefilter"].split("|")
            entity: dict[str, Any] = (
                {"id": params["ids"], "missing": ""}
                if self.missing
                else {
                    "id": params["ids"],
                    "sitelinks": {
                        site: {
                            "site": site,
                            "title": title,
                            "url": f"https://{site[:-4].replace('_', '-')}.wikipedia.org/wiki/{title}",
                        }
                        for site, title in self.sitelinks.items()
                        if site in wanted
                    },
                }
            )
            return httpx.Response(200, json={"entities": {params["ids"]: entity}})
        page = {
            "pageid": 1,
            "title": params["titles"],
            "extract": self.extract,
            "revisions": [{"revid": 1384249363, "parentid": 1}],
        }
        return httpx.Response(200, json={"query": {"pages": [page]}})


def fetch(service: Wikimedia, languages: list[str]) -> wikimedia.Biography | None:
    with httpx.Client(transport=httpx.MockTransport(service)) as client:
        return wikimedia.biography(ITEM, languages=languages, user_agent=AGENT, client=client)


def test_the_biography_is_the_articles_introduction_in_the_first_language_it_has():
    service = Wikimedia({"enwiki": "Radiohead", "dewiki": "Radiohead"})
    found = fetch(service, ["pt", "en", "de"])
    assert found == wikimedia.Biography(
        text=INTRO,
        language="en",
        url="https://en.wikipedia.org/wiki/Radiohead",
        revision="1384249363",
    )
    item, article = service.requests
    assert item.url.params["ids"] == ITEM
    assert item.url.params["sitefilter"] == "ptwiki|enwiki|dewiki"
    assert article.url.host == "en.wikipedia.org"
    assert article.url.params["titles"] == "Radiohead"
    assert article.url.params["exintro"] and article.url.params["explaintext"]
    assert all(r.headers["User-Agent"] == AGENT for r in service.requests)


def test_a_language_code_with_a_hyphen_names_its_site_with_an_underscore():
    service = Wikimedia({"zh_yuewiki": "Radiohead"})
    assert fetch(service, ["zh-yue"]) is not None
    assert service.requests[0].url.params["sitefilter"] == "zh_yuewiki"


def test_the_text_is_trimmed():
    assert fetch(Wikimedia({"enwiki": "Radiohead"}, extract=f"\n{INTRO}\n\n"), ["en"]).text == INTRO


def test_no_article_in_any_language_is_no_biography():
    service = Wikimedia({"dewiki": "Radiohead"})
    assert fetch(service, ["en", "pt"]) is None
    assert len(service.requests) == 1


def test_an_item_wikidata_no_longer_has_is_no_biography():
    assert fetch(Wikimedia({}, missing=True), ["en"]) is None


def test_an_article_with_no_introduction_is_no_biography():
    assert fetch(Wikimedia({"enwiki": "Radiohead"}, extract="  "), ["en"]) is None


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(503, text="Service Unavailable"),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json={"error": {"code": "maxlag"}}),
    ],
    ids=["unavailable", "not-json", "error"],
)
def test_an_answer_that_cant_be_read_is_an_error_not_an_absence(answer):
    with (
        httpx.Client(transport=httpx.MockTransport(lambda _request: answer)) as client,
        pytest.raises(wikimedia.WikimediaError),
    ):
        wikimedia.biography(ITEM, languages=["en"], user_agent=AGENT, client=client)


def test_an_unreachable_service_is_an_error():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(refuse)) as client,
        pytest.raises(wikimedia.WikimediaError),
    ):
        wikimedia.biography(ITEM, languages=["en"], user_agent=AGENT, client=client)


def test_an_article_somewhere_other_than_wikipedia_is_not_asked():
    """The article's address comes from Wikidata's answer, so a host that isn't
    a Wikipedia is refused rather than sent a request."""

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.host != "www.wikidata.org":
            raise AssertionError(f"asked {request.url.host}")
        sitelink = {"site": "enwiki", "title": "X", "url": "https://example.com/wiki/X"}
        entity = {"id": ITEM, "sitelinks": {"enwiki": sitelink}}
        return httpx.Response(200, content=json.dumps({"entities": {ITEM: entity}}))

    with (
        httpx.Client(transport=httpx.MockTransport(answer)) as client,
        pytest.raises(wikimedia.WikimediaError),
    ):
        wikimedia.biography(ITEM, languages=["en"], user_agent=AGENT, client=client)
