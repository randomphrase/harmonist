"""An artist's biography from Wikipedia, found through MusicBrainz (#1018).

The article is reached only by following links: MusicBrainz's link from the
artist to its Wikidata item, then that item's link to an article in each
Wikipedia. Nothing is matched by name, so a biography is about whoever
MusicBrainz says the artist is, and a wrong one is put right on MusicBrainz or
on Wikipedia rather than here.

Nothing is cached. A biography is fetched only when the user identifies an
artist, which writes it into the file, and the file is then where it lives.

Wikimedia asks every client to identify itself with a contact address
(https://meta.wikimedia.org/wiki/User-Agent_policy), which the MusicBrainz user
agent Harmonist is configured with already does.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

#: How long one request may take. A biography is fetched while the user waits
#: for an identification, so a slow answer has to end in an error they can
#: retry rather than a page that never comes back.
TIMEOUT = httpx.Timeout(10.0)

_WIKIDATA_API = "https://www.wikidata.org/w/api.php"
_ITEM_URL_RE = re.compile(r"https?://(?:www\.|m\.)?wikidata\.org/wiki/(Q[1-9][0-9]*)/?$")
_WIKIPEDIA_HOST_RE = re.compile(r"[a-z][a-z0-9-]*\.wikipedia\.org")


class WikimediaError(Exception):
    """Wikidata or Wikipedia couldn't be asked, or answered something that
    can't be read. Never "no biography": that is `None` from `biography`."""


@dataclass(frozen=True)
class Biography:
    """An article's introduction, and which article and revision it is."""

    text: str
    language: str
    url: str
    revision: str


def wikidata_item(artist: Mapping[str, Any]) -> str | None:
    """The Wikidata item MusicBrainz links `artist` to, as its ID (`Q44190`).

    None without exactly one current link: an ended link is no longer the
    artist's, and choosing between two would be a guess."""
    items = [
        found.group(1)
        for relation in artist.get("url-relation-list") or ()
        if relation.get("type") == "wikidata"
        and relation.get("ended") != "true"
        and (found := _ITEM_URL_RE.match(str(relation.get("target", ""))))
    ]
    return items[0] if len(items) == 1 else None


def biography(
    item: str,
    *,
    languages: Sequence[str],
    user_agent: str,
    client: httpx.Client | None = None,
) -> Biography | None:
    """The introduction of the article Wikidata's `item` links to in the first
    of `languages` that has one, as plain text.

    None when no Wikipedia in `languages` has an article for the item, or the
    article has no introduction. Raises `WikimediaError` when either service
    can't be asked or answers something unreadable.
    """
    if client is None:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as owned:
            return biography(item, languages=languages, user_agent=user_agent, client=owned)
    headers = {"User-Agent": user_agent}
    sites = {_site(language): language for language in languages}
    entities = _ask(
        client,
        _WIKIDATA_API,
        {
            "action": "wbgetentities",
            "ids": item,
            "props": "sitelinks/urls",
            "sitefilter": "|".join(sites),
            "format": "json",
        },
        headers,
    ).get("entities")
    if not isinstance(entities, dict):
        raise WikimediaError("Wikidata's answer has no entities")
    # Wikidata follows a merged item to the one it became, under that one's ID.
    entity = next(iter(entities.values()), None)
    sitelinks = entity.get("sitelinks") if isinstance(entity, dict) else None
    if not isinstance(sitelinks, dict):
        return None
    site = next((site for site in sites if site in sitelinks), None)
    if site is None:
        return None
    link = sitelinks[site]
    url, title = str(link.get("url", "")), str(link.get("title", ""))
    host = urlsplit(url).hostname or ""
    # The article's address is Wikidata's answer, so it must be a Wikipedia's
    # before anything is sent there.
    if not _WIKIPEDIA_HOST_RE.fullmatch(host) or not title:
        raise WikimediaError(f"Wikidata links {item} to {url!r}, which isn't a Wikipedia article")
    query = _ask(
        client,
        f"https://{host}/w/api.php",
        {
            "action": "query",
            "prop": "extracts|revisions",
            "exintro": "1",
            "explaintext": "1",
            "rvprop": "ids",
            "titles": title,
            "redirects": "1",
            "format": "json",
            "formatversion": "2",
        },
        headers,
    ).get("query")
    pages = query.get("pages") if isinstance(query, dict) else None
    if not isinstance(pages, list) or not pages or not isinstance(pages[0], dict):
        raise WikimediaError(f"{host} answered with no page for {title!r}")
    page = pages[0]
    if page.get("missing"):
        return None
    text = str(page.get("extract") or "").strip()
    revisions = page.get("revisions")
    if not text:
        return None
    if not isinstance(revisions, list) or not revisions or "revid" not in revisions[0]:
        raise WikimediaError(f"{host} gave no revision for {title!r}")
    return Biography(text=text, language=sites[site], url=url, revision=str(revisions[0]["revid"]))


def _site(language: str) -> str:
    """Wikidata's name for a language's Wikipedia: `en` -> `enwiki`, `zh-yue`
    -> `zh_yuewiki`."""
    return language.replace("-", "_") + "wiki"


def _ask(
    client: httpx.Client, url: str, params: Mapping[str, str], headers: Mapping[str, str]
) -> dict[str, Any]:
    try:
        response = client.get(url, params=params, headers=headers)
        response.raise_for_status()
        answer = response.json()
    except httpx.HTTPError as exc:
        raise WikimediaError(f"{urlsplit(url).hostname}: {exc}") from exc
    except ValueError as exc:
        raise WikimediaError(f"{urlsplit(url).hostname} answered with no JSON") from exc
    if not isinstance(answer, dict):
        raise WikimediaError(f"{urlsplit(url).hostname} answered with no object")
    if "error" in answer:
        raise WikimediaError(f"{urlsplit(url).hostname}: {answer['error']}")
    return answer
