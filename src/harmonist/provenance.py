"""Where an album's files came from, judged by their own tags (#632).

Only positive evidence counts. A store's own tags establish a download and
AccurateRip's establish a rip; a UPC on its own establishes neither (CD rippers
write one too), nor does hi-res audio (so is a vinyl or Blu-ray rip) or a CD
table of contents (XLD synthesizes one on any transcode). docs/design.md keeps
the table of what each tag proves.
"""

from enum import StrEnum

from .formats.types import ProvenanceTags


class Store(StrEnum):
    """A store whose own tags prove a file was bought from it as a download.

    Bandcamp isn't one: its evidence is a URL in the comment
    (`url_recovery`), which names the release as well as the store."""

    QOBUZ = "Qobuz"
    AMAZON = "Amazon"
    BEATPORT = "Beatport"


def stores(tags: ProvenanceTags) -> frozenset[Store]:
    """The stores this one file's tags name, each by the mark it leaves."""
    found = {Store.QOBUZ} if tags.qobuz_track_id else set()
    for comment in tags.comments:
        text = comment.strip()
        if text.startswith("Amazon.com Song ID:"):
            found.add(Store.AMAZON)
        elif text.casefold() == "purchased at beatport.com":
            found.add(Store.BEATPORT)
    return frozenset(found)
