"""An artist's picture from fanart.tv, by the artist's MusicBrainz ID (#1020).

fanart.tv files its artist thumbnails, square and usually 1000 pixels a side,
under the artist's MBID, so the picture is about whoever MusicBrainz says the
artist is and nothing is matched by name. Only the first thumbnail is used:
choosing among several would be a guess.

Every request carries Harmonist's project key, and a user's personal key when
they have one, which gets them newer images (two days old rather than seven).
Both go in headers rather than the address, so neither turns up anywhere an
address is logged. The project key is public by design (see
`docs/design/external-services.md`).

Nothing is cached. A picture is fetched only when the user identifies an artist
or reads it again, which writes it beside the NFO, and the folder is then where
it lives.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit

import httpx

#: Harmonist's project key, registered for it with fanart.tv by its maintainer.
#: It isn't a secret and can't be one: every copy of Harmonist carries it, as
#: every copy of Kodi and Jellyfin carries theirs. It says which project asked,
#: and opens nothing that isn't public. Moving it out of the source would hide
#: it from nobody.
PROJECT_KEY: Final = "995a4425378642b9355bd248421f9623"

#: How long one request may take. A picture is fetched while the user waits
#: for an identification, so a slow answer has to end in an error they can
#: retry rather than a page that never comes back.
TIMEOUT = httpx.Timeout(10.0)

_API = "https://webservice.fanart.tv/v3.2/music/{mbid}"
_PAGE = "https://fanart.tv/artist/{mbid}/"
_ASSET_HOST = "assets.fanart.tv"


class FanartError(Exception):
    """fanart.tv couldn't be asked, or answered something that can't be used.
    Never "no picture": that is `None` from `artist_thumb`."""


@dataclass(frozen=True)
class Thumb:
    """One of fanart.tv's artist thumbnails, and what it says about whose it
    is."""

    url: str
    #: fanart.tv's page for the artist, which the credit links to.
    page: str
    #: The copyright holder fanart.tv has recorded, if it has one.
    copyright: str = ""


def available(personal_key: str) -> bool:
    """Whether fanart.tv can be asked at all: it answers nothing without a key."""
    return bool(PROJECT_KEY or personal_key)


async def artist_thumb(
    mbid: str, *, personal_key: str, user_agent: str, client: httpx.AsyncClient
) -> Thumb | None:
    """The first of the artist `mbid`'s thumbnails on fanart.tv.

    None when fanart.tv has no artist with that ID, or no thumbnail for it.
    Raises `FanartError` when it can't be asked or answers something
    unreadable, including a thumbnail that isn't on its own image host.
    """
    try:
        response = await client.get(
            _API.format(mbid=mbid), headers=_headers(personal_key, user_agent), timeout=TIMEOUT
        )
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        response.raise_for_status()
        answer: Any = response.json()
    except httpx.HTTPError as exc:
        raise FanartError(f"fanart.tv: {exc}") from exc
    except ValueError as exc:
        raise FanartError("fanart.tv answered with no JSON") from exc
    if not isinstance(answer, dict):
        raise FanartError("fanart.tv answered with no object")
    thumbs = answer.get("artistthumb") or []
    if not isinstance(thumbs, list) or not thumbs:
        return None
    first = thumbs[0]
    url = str(first.get("url", "")) if isinstance(first, dict) else ""
    _check_host(url)
    holder = str(first.get("copyright") or "").strip()
    return Thumb(
        url=url,
        page=_PAGE.format(mbid=mbid),
        copyright="" if holder.casefold() == "unknown" else holder,
    )


def _headers(personal_key: str, user_agent: str) -> dict[str, str]:
    headers = {"User-Agent": user_agent}
    if PROJECT_KEY:
        headers["api-key"] = PROJECT_KEY
    if personal_key:
        headers["client-key"] = personal_key
    return headers


def _check_host(url: str) -> None:
    """A picture's address is fanart.tv's answer, so it must be on fanart.tv's
    own image host before anything is sent there."""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != _ASSET_HOST:
        raise FanartError(f"fanart.tv gave {url!r}, which isn't on {_ASSET_HOST}")
