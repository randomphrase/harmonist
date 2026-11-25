"""An artist's biography from Wikipedia (#1018), and their picture from
Wikimedia Commons (#1020), found through MusicBrainz.

Each is reached only by following links: MusicBrainz's link from the artist to
its Wikidata item, then that item's link to an article in each Wikipedia, or
its image (P18) on Commons. Nothing is matched by name, so a biography or a
picture is about whoever MusicBrainz says the artist is, and a wrong one is put
right on MusicBrainz or on Wikidata rather than here.

Nothing is cached. Each is fetched only when the user identifies an artist or
reads it again, which writes it into the file or beside it, and the folder is
then where it lives.

Wikimedia asks every client to identify itself with a contact address
(https://meta.wikimedia.org/wiki/User-Agent_policy), which the MusicBrainz user
agent Harmonist is configured with already does.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

import httpx

#: How long one request may take. A biography is fetched while the user waits
#: for an identification, so a slow answer has to end in an error they can
#: retry rather than a page that never comes back.
TIMEOUT = httpx.Timeout(10.0)

_WIKIDATA_API = "https://www.wikidata.org/w/api.php"
_COMMONS_API = "https://commons.wikimedia.org/w/api.php"
_ITEM_URL_RE = re.compile(r"https?://(?:www\.|m\.)?wikidata\.org/wiki/(Q[1-9][0-9]*)/?$")
_WIKIPEDIA_HOST_RE = re.compile(r"[a-z][a-z0-9-]*\.wikipedia\.org")
#: Where Commons keeps its files: originals on the first, renditions on the
#: second.
_IMAGE_HOSTS = frozenset({"upload.wikimedia.org", "thumb.wikimedia.org"})
_COMMONS_HOST = "commons.wikimedia.org"

#: How wide a rendition of a Commons picture is fetched. An original can be a
#: forty-megapixel photograph, and no player draws an artist wider than
#: fanart.tv's thumbnails, 1000 pixels. Commons renders only standard widths and
#: gives the next one up for any other, so 1000 would fetch 1280; 960 is the
#: widest of them within 1000.
PICTURE_WIDTH = 960


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


@dataclass(frozen=True)
class CommonsPicture:
    """A Wikidata item's image on Commons, as a rendition to fetch, and what
    its credit needs: Commons pictures are CC BY or CC BY-SA, or free of
    copyright, and the first two ask for the author and the licence."""

    url: str
    #: The file's page on Commons, which the credit links to.
    page: str
    author: str
    licence: str
    licence_url: str


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


async def biography(
    item: str,
    *,
    languages: Sequence[str],
    user_agent: str,
    client: httpx.AsyncClient,
) -> Biography | None:
    """The introduction of the article Wikidata's `item` links to in the first
    of `languages` that has one, as plain text.

    None when no Wikipedia in `languages` has an article for the item, or the
    article has no introduction. Raises `WikimediaError` when either service
    can't be asked or answers something unreadable.
    """
    headers = {"User-Agent": user_agent}
    sites = {_site(language): language for language in languages}
    entities = (
        await _ask(
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
        )
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
    query = (
        await _ask(
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
        )
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


async def picture(
    item: str, *, user_agent: str, client: httpx.AsyncClient
) -> CommonsPicture | None:
    """The image Wikidata gives `item` (P18), as Commons' rendition
    `PICTURE_WIDTH` pixels wide, with its author and licence.

    The best-ranked claim, and only when there is exactly one: an item with
    two preferred images, or two normal ones and no preferred, has no picture
    rather than a guess. None also when the item, or the file it names, isn't
    there. Raises `WikimediaError` when either service can't be asked or
    answers something unreadable, including a rendition that isn't on one of
    Wikimedia's own image hosts.
    """
    headers = {"User-Agent": user_agent}
    claims = (
        await _ask(
            client,
            _WIKIDATA_API,
            {"action": "wbgetclaims", "entity": item, "property": "P18", "format": "json"},
            headers,
            empty=("no-such-entity",),
        )
    ).get("claims")
    name = _best_value((claims or {}).get("P18") if isinstance(claims, dict) else None)
    if name is None:
        return None
    query = (
        await _ask(
            client,
            _COMMONS_API,
            {
                "action": "query",
                "titles": f"File:{name}",
                "prop": "imageinfo",
                "iiprop": "url|extmetadata",
                "iiurlwidth": str(PICTURE_WIDTH),
                "iiextmetadatafilter": "Artist|LicenseShortName|LicenseUrl",
                "format": "json",
                "formatversion": "2",
            },
            headers,
        )
    ).get("query")
    pages = query.get("pages") if isinstance(query, dict) else None
    if not isinstance(pages, list) or not pages or not isinstance(pages[0], dict):
        raise WikimediaError(f"Commons answered with no page for {name!r}")
    page = pages[0]
    infos = page.get("imageinfo")
    if page.get("missing") or not isinstance(infos, list) or not infos:
        return None
    info = infos[0] if isinstance(infos[0], dict) else {}
    url = str(info.get("thumburl") or info.get("url") or "")
    parts = urlsplit(url)
    # The rendition's address is Commons' answer, so it must be on one of
    # Wikimedia's image hosts before anything is sent there.
    if parts.scheme != "https" or parts.hostname not in _IMAGE_HOSTS:
        raise WikimediaError(f"Commons gave {url!r} for {name!r}, which isn't Wikimedia's")
    page_url = str(info.get("descriptionurl") or "")
    page_parts = urlsplit(page_url)
    # The credit links to the file's page, so it must be Commons' own.
    if page_parts.scheme != "https" or page_parts.hostname != _COMMONS_HOST:
        raise WikimediaError(f"Commons gave {page_url!r} as the page for {name!r}")
    metadata = info.get("extmetadata")
    said = metadata if isinstance(metadata, dict) else {}

    def value_of(key: str) -> str:
        entry = said.get(key)
        return str(entry.get("value") or "") if isinstance(entry, dict) else ""

    licence_url = value_of("LicenseUrl").strip()
    return CommonsPicture(
        url=url,
        page=page_url,
        author=_plain(value_of("Artist")),
        licence=_plain(value_of("LicenseShortName")),
        # Whoever wrote the file's page wrote this, and the credit links it:
        # an address of any other kind is no licence's.
        licence_url=licence_url if urlsplit(licence_url).scheme in ("http", "https") else "",
    )


def _best_value(statements: object) -> str | None:
    """The one value among the best-ranked of a property's statements, or
    None when there isn't exactly one. Wikidata ranks a statement preferred,
    normal or deprecated: a deprecated one is known to be wrong, and a
    preferred one outranks the normal ones."""
    if not isinstance(statements, list):
        return None
    current = [s for s in statements if isinstance(s, dict) and s.get("rank") != "deprecated"]
    best = [s for s in current if s.get("rank") == "preferred"] or current
    values = []
    for statement in best:
        snak = statement.get("mainsnak")
        datavalue = snak.get("datavalue") if isinstance(snak, dict) else None
        value = datavalue.get("value") if isinstance(datavalue, dict) else None
        if isinstance(value, str) and value:
            values.append(value)
    return values[0] if len(best) == 1 and len(values) == 1 else None


def _plain(markup: str) -> str:
    """Commons' metadata is HTML (an author is often a link to their user
    page): its text, on one line."""
    reader = _TextReader()
    reader.feed(markup)
    reader.close()
    return " ".join("".join(reader.parts).split())


class _TextReader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _site(language: str) -> str:
    """Wikidata's name for a language's Wikipedia: `en` -> `enwiki`, `zh-yue`
    -> `zh_yuewiki`."""
    return language.replace("-", "_") + "wiki"


async def _ask(
    client: httpx.AsyncClient,
    url: str,
    params: Mapping[str, str],
    headers: Mapping[str, str],
    *,
    empty: Sequence[str] = (),
) -> dict[str, Any]:
    """The service's answer, raising `WikimediaError` for an error it gives,
    except one whose code is in `empty`: an answer of nothing, read as an empty
    one."""
    try:
        response = await client.get(url, params=params, headers=headers, timeout=TIMEOUT)
        response.raise_for_status()
        answer = response.json()
    except httpx.HTTPError as exc:
        raise WikimediaError(f"{urlsplit(url).hostname}: {exc}") from exc
    except ValueError as exc:
        raise WikimediaError(f"{urlsplit(url).hostname} answered with no JSON") from exc
    if not isinstance(answer, dict):
        raise WikimediaError(f"{urlsplit(url).hostname} answered with no object")
    if "error" in answer:
        error = answer["error"]
        if isinstance(error, dict) and error.get("code") in empty:
            return {}
        raise WikimediaError(f"{urlsplit(url).hostname}: {error}")
    return answer
