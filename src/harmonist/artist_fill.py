"""What an artist NFO lacks that Harmonist can fill in, and why it couldn't.

Identifying an artist (#1018) and reading it from MusicBrainz again (#1022)
both fill in a biography where the file has none, from the Wikipedia article
MusicBrainz's Wikidata link leads to, and a picture where the folder has none
(#1020), from fanart.tv or else the image Wikidata gives the same item. What
each lookup found is kept beside the artist's stored answer, so the artist page
can say why there is none without asking anyone when it is drawn.

The lookups don't wait on each other where they needn't. fanart.tv needs only
the artist's ID, so it is asked while MusicBrainz is; the biography and the
Commons picture both follow MusicBrainz's answer, and are asked side by side.
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx

from . import activity_store, artist_nfo, fanart_tv, images, nfo_history, wikimedia

log = logging.getLogger(__name__)

#: Keeps a failed lookup's detail out of the Activity feed: the action it was
#: part of reports the failure there itself.
_LOG_ONLY = {"_activity": True}

#: How long fetching a picture may take, as for the lookups that found it.
_TIMEOUT = httpx.Timeout(10.0)

_T = TypeVar("_T")


class PictureFetchError(Exception):
    """A picture a service named couldn't be fetched, or is too large to
    write."""


@dataclass(frozen=True)
class Asking:
    """What the lookups are asked with, from the configuration."""

    languages: Sequence[str]
    user_agent: str
    #: The user's own fanart.tv key, which gets them newer pictures. Optional.
    fanart_key: str = ""


@dataclass(frozen=True)
class Filled:
    """What the lookups found to fill in: the biography, or none, and whether
    its lookup failed rather than found nothing; and the picture, or none, and
    which service failed if one did."""

    biography: wikimedia.Biography | None = None
    failed: bool = False
    picture: nfo_history.NewPicture | None = None
    picture_failed: str | None = None

    @property
    def wanted(self) -> dict[str, str]:
        """What to write into the file: the biography as HTML, the form the
        players read it in."""
        found = self.biography
        return {"biography": artist_nfo.biography_html(found.text)} if found else {}

    @property
    def credits(self) -> dict[str, nfo_history.Credit]:
        found = self.biography
        return {"biography": nfo_history.Credit(found.url, found.revision)} if found else {}


@dataclass(frozen=True)
class _Found:
    """What one picture lookup found: a picture, none, or a failure."""

    picture: nfo_history.NewPicture | None = None
    failed: str | None = None


def fill(
    requested: str,
    read: Callable[[], Mapping[str, Any]],
    *,
    lacks_biography: bool,
    lacks_picture: bool,
    asking: Asking,
) -> tuple[Mapping[str, Any], Filled]:
    """MusicBrainz's answer for the artist `requested`, from `read`, and what
    to fill into its NFO from elsewhere: a biography where the file lacks one,
    and a picture where its folder does.

    fanart.tv is asked while MusicBrainz is. If MusicBrainz answers with
    another artist, the one `requested` was merged into, fanart.tv is asked
    about that one instead. Raises whatever `read` raises, and then nothing
    else is waited for.
    """
    with _side_by_side() as submit:
        early = (
            submit(_fanart, requested, asking)
            if lacks_picture and fanart_tv.available(asking.fanart_key)
            else None
        )
        answer = read()
        mbid = str(answer.get("id") or requested)
        if early is not None and mbid != requested:
            early.cancel()
            early = submit(_fanart, mbid, asking)
        told = (
            submit(
                biography,
                mbid,
                answer,
                languages=asking.languages,
                user_agent=asking.user_agent,
            )
            if lacks_biography
            else None
        )
        found = _picture(mbid, answer, early, asking) if lacks_picture else _Found()
        said = told.result() if told is not None else Filled()
    return answer, Filled(said.biography, said.failed, found.picture, found.failed)


@contextmanager
def _side_by_side() -> Iterator[Callable[..., Future[Any]]]:
    """Submit lookups to run beside this thread, for one identification.

    Each runs in a copy of this thread's context, so it belongs to the action
    this one does (`activity_store.action`), which a thread doesn't inherit.
    Leaving doesn't wait: a lookup still running when MusicBrainz couldn't be
    read is let finish and its answer dropped, rather than keeping the user
    waiting for an answer nothing will use. How Harmonist runs work side by
    side is under review as a whole; this is the one place a change of
    approach touches here.
    """
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="harmonist-artist")

    def submit(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> Future[_T]:
        context = contextvars.copy_context()
        return pool.submit(lambda: context.run(fn, *args, **kwargs))

    try:
        yield submit
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _fanart(mbid: str, asking: Asking) -> _Found:
    """fanart.tv's picture of the artist `mbid`, fetched."""
    try:
        thumb = fanart_tv.artist_thumb(
            mbid, personal_key=asking.fanart_key, user_agent=asking.user_agent
        )
        data = download_picture(thumb.url, user_agent=asking.user_agent) if thumb else None
    except (fanart_tv.FanartError, PictureFetchError) as exc:
        log.warning(
            "could not fetch a picture of %s from fanart.tv: %s", mbid, exc, extra=_LOG_ONLY
        )
        return _Found(failed="fanart.tv")
    if thumb is None or data is None:
        return _Found()
    credit = nfo_history.PictureCredit("fanart.tv", thumb.page, author=thumb.copyright)
    return _Found(nfo_history.NewPicture(data, credit))


def _picture(
    mbid: str, answer: Mapping[str, Any], early: Future[_Found] | None, asking: Asking
) -> _Found:
    """The picture to fill in: fanart.tv's, or failing that, the image
    Wikidata gives the item MusicBrainz links the artist to. A failure at
    fanart.tv is a failure, not a reason to take Commons' picture instead:
    filling in only where there is none means the second choice would stand
    for good. What it found is kept with the artist's stored answer."""
    if early is not None:
        found = early.result()
        if found.picture is not None or found.failed:
            _keep_picture(mbid, found, source="fanart.tv")
            return found
    item = wikimedia.wikidata_item(answer)
    if item is None:
        activity_store.store_artist_picture(mbid, {"status": "missing", "item": None})
        return _Found()
    try:
        found_on_commons = wikimedia.picture(item, user_agent=asking.user_agent)
        data = (
            download_picture(found_on_commons.url, user_agent=asking.user_agent)
            if found_on_commons
            else None
        )
    except (wikimedia.WikimediaError, PictureFetchError) as exc:
        log.warning("could not fetch a picture of %s from Commons: %s", item, exc, extra=_LOG_ONLY)
        found = _Found(failed="Wikimedia Commons")
        _keep_picture(mbid, found, source="Wikimedia Commons")
        return found
    if found_on_commons is None or data is None:
        activity_store.store_artist_picture(mbid, {"status": "missing", "item": item})
        return _Found()
    found = _Found(
        nfo_history.NewPicture(
            data,
            nfo_history.PictureCredit(
                "Wikimedia Commons",
                found_on_commons.page,
                author=found_on_commons.author,
                licence=found_on_commons.licence,
                licence_url=found_on_commons.licence_url,
            ),
        )
    )
    _keep_picture(mbid, found, source="Wikimedia Commons")
    return found


def _keep_picture(mbid: str, found: _Found, *, source: str) -> None:
    activity_store.store_artist_picture(
        mbid,
        {"status": "failed", "service": source}
        if found.failed
        else {"status": "found", "service": source},
    )


def download_picture(
    url: str, *, user_agent: str, client: httpx.Client | None = None
) -> bytes | None:
    """The picture at `url`, an address a service's answer gave and its module
    checked, or None when it isn't a JPEG or a PNG, the only pictures written.
    A redirect isn't followed, since it could lead anywhere. Raises
    `PictureFetchError` when it can't be fetched or is over the size Harmonist
    shows."""
    if client is None:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=False) as owned:
            return download_picture(url, user_agent=user_agent, client=owned)
    data = bytearray()
    try:
        with client.stream("GET", url, headers={"User-Agent": user_agent}) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                data += chunk
                if len(data) > artist_nfo.IMAGE_LIMIT:
                    raise PictureFetchError(f"{url} is over {artist_nfo.IMAGE_LIMIT} bytes")
    except httpx.HTTPError as exc:
        raise PictureFetchError(f"{url}: {exc}") from exc
    return bytes(data) if images.mime_of(bytes(data)) is not None else None


def biography(
    mbid: str, answer: Mapping[str, Any], *, languages: Sequence[str], user_agent: str
) -> Filled:
    """The biography for the artist MusicBrainz answered with as `answer`: the
    introduction of the article its Wikidata item links to, in the first of
    `languages` that has one. Never found by name, so without a Wikidata link
    on MusicBrainz there is none, and Wikipedia isn't asked.

    What it found is kept with the artist's stored answer, which the caller has
    stored already."""
    item = wikimedia.wikidata_item(answer)
    if item is None:
        return Filled()
    try:
        found = wikimedia.biography(item, languages=languages, user_agent=user_agent)
    except wikimedia.WikimediaError as exc:
        log.warning("could not fetch a biography for %s: %s", item, exc, extra=_LOG_ONLY)
        _keep(mbid, item, languages, "failed")
        return Filled(failed=True)
    _keep(mbid, item, languages, "found" if found else "missing")
    return Filled(biography=found)


def note(stored: activity_store.CachedArtist | None) -> str | None:
    """Why a file with no biography has none, from the artist's stored answer:
    the last word on it, as of when MusicBrainz was read. None when nothing
    says why, such as an artist never read, or a lookup about an item
    MusicBrainz no longer links."""
    if stored is None:
        return None
    item = wikimedia.wikidata_item(stored.payload)
    if item is None:
        return "MusicBrainz links no Wikidata item"
    lookup = stored.biography or {}
    if lookup.get("item") != item:
        return None
    if lookup.get("status") == "missing":
        return f"No Wikipedia article in {' or '.join(lookup.get('languages') or ())}"
    if lookup.get("status") == "failed":
        return "Wikipedia couldn't be reached"
    return None


def unlinked(stored: activity_store.CachedArtist | None) -> bool:
    """Whether MusicBrainz, as last read, links the artist to no Wikidata item:
    the one reason for no biography the user can put right, on MusicBrainz
    (#1025). False for an artist never read, since nothing says so yet."""
    return stored is not None and wikimedia.wikidata_item(stored.payload) is None


def picture_note(stored: activity_store.CachedArtist | None) -> str | None:
    """Why an identified artist has no picture, from what the last read
    found (#1020). None when nothing says why: an artist not read since
    pictures were looked up, or one whose picture was found and has since
    been taken away."""
    lookup = (stored.picture if stored else None) or {}
    status = lookup.get("status")
    if status == "failed":
        return f"{lookup.get('service') or 'A service'} couldn't be reached"
    if status == "missing":
        if lookup.get("item"):
            return "None on fanart.tv or Wikimedia Commons"
        return "None on fanart.tv, and MusicBrainz links no Wikidata item"
    return None


def _keep(mbid: str, item: str, languages: Sequence[str], status: str) -> None:
    activity_store.store_artist_biography(
        mbid, {"item": item, "languages": list(languages), "status": status}
    )
