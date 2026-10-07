"""The album page's Additional info: the origin, then the tags it rests on (#634)."""

import pytest
from bs4 import BeautifulSoup

from harmonist import provenance, scanner, sidecar
from harmonist.models import Sidecar
from harmonist.provenance import Origin
from harmonist.url_recovery import link_parts
from test.helpers import ACCURATERIP_TAGS, BEATPORT_TAGS, QOBUZ_TAGS, write_provenance_tags
from test.test_upc_contributions import MBID, UPC, download
from test.test_web import _id_for, _make_album, cfg, client  # noqa: F401  (fixtures)


def info(folder):
    return provenance.info(scanner.scan(folder.parent)[0])


def rows(found):
    return {row.tag: row for row in found.rows}


def test_a_tag_on_every_track_with_its_own_value_says_so(tmp_path):
    folder = download(tmp_path, (UPC, UPC, UPC), marks={})
    per_track = "AccurateRip: Accurate (confidence 28)   [0000000{n}]"
    write_provenance_tags(folder, ".m4a", {"AccurateRipResult": (per_track,)})
    found = info(folder)
    assert found.origin is Origin.CD
    row = rows(found)["AccurateRipResult"]
    assert row.every_differs
    assert [value for _, value in row.tracks] == [
        f"AccurateRip: Accurate (confidence 28)   [0000000{n}]" for n in (1, 2, 3)
    ]
    # The same value on every track is the tag's value, plainly.
    assert rows(found)["UPC"].consensus.is_unanimous


def test_a_tag_short_of_every_track_differing_keeps_the_consensus(tmp_path):
    """Two tracks agree and the third lacks the tag: a majority to show, and
    the Album section's model says the rest — not "differs on every track"."""
    folder = download(tmp_path, (UPC, UPC, None), marks=QOBUZ_TAGS)
    row = rows(info(folder))["UPC"]
    assert not row.every_differs
    assert row.consensus.value == UPC
    assert row.consensus.odd_summary == "missing on 1 track"


def test_every_comment_frame_gets_its_own_row_in_order(tmp_path):
    folder = download(tmp_path, ext=".mp3", marks=BEATPORT_TAGS)
    found = info(folder)
    assert found.origin is Origin.BEATPORT
    comments = [row.consensus.value for row in found.rows if row.tag == "Comment"]
    assert comments == ["Techno - Techno", "Purchased at Beatport.com"]


def test_conflicting_evidence_is_an_unknown_origin_that_says_why(tmp_path):
    folder = download(tmp_path, marks=QOBUZ_TAGS)
    write_provenance_tags(folder, ".m4a", ACCURATERIP_TAGS)
    found = info(folder)
    assert found.origin is Origin.UNKNOWN
    assert found.why == "Its tags point to more than one origin."


def test_harmonists_own_download_is_bandcamp_without_a_tag_to_show(tmp_path):
    folder = download(tmp_path, (None,), marks={})
    assert info(folder) is None
    sidecar.write(folder, Sidecar(mb_release_id=MBID, bandcamp_downloaded=True))
    found = info(folder)
    assert found.origin is Origin.BANDCAMP
    assert found.why == "Harmonist downloaded it from Bandcamp."
    assert found.rows == ()


@pytest.mark.parametrize(
    ("text", "parts"),
    [
        (
            "Visit https://artist.bandcamp.com/album/x",
            [("Visit ", None), ("https://artist.bandcamp.com/album/x",) * 2],
        ),
        # Trailing punctuation is prose, not part of the URL.
        (
            "see (https://x.bandcamp.com/album/y).",
            [("see (", None), ("https://x.bandcamp.com/album/y",) * 2, (").", None)],
        ),
        # A `www.` host is a link without its scheme; a bare domain isn't one,
        # or "Amazon.com Song ID" would be.
        ("Lyd AS. www.2L.no", [("Lyd AS. ", None), ("www.2L.no", "https://www.2L.no")]),
        ("Amazon.com Song ID: 207119654", [("Amazon.com Song ID: 207119654", None)]),
    ],
)
def test_link_parts_links_the_urls_in_a_comment_and_not_its_prose(text, parts):
    assert link_parts(text) == parts


def test_album_page_shows_the_origin_and_links_only_the_comments_url(client, cfg):  # noqa: F811
    url = "https://artist.bandcamp.com/album/record"
    d = _make_album(cfg, "Linked", comment=f"Visit {url}")
    page = BeautifulSoup(client.get(f"/album/{_id_for(cfg, d)}").text, "html.parser")
    section = page.select_one("#album-info")
    assert section is not None
    origin = page.select_one("#album-identity dd[data-field='origin'] span")
    assert origin is not None
    assert origin.get_text(strip=True) == "Bandcamp"
    assert origin["title"] == "Its comment carries a Bandcamp URL."
    (tag, value) = section.select(".info-tags > tbody > tr > td")
    assert tag.get_text() == "Comment"
    assert [a["href"] for a in value.select("a")] == [url]
    assert value.select_one("a").get_text() == url  # the URL, not "Visit …"


@pytest.mark.parametrize("ripped", [False, True])
def test_origin_links_only_its_own_store(client, cfg, ripped):  # noqa: F811
    url = "https://artist.bandcamp.com/album/record"
    d = _make_album(cfg, "Origin", mbid=MBID)
    sidecar.write(d, Sidecar(mb_release_id=MBID, store_url=url, bandcamp_downloaded=not ripped))
    if ripped:
        write_provenance_tags(d, ".m4a", ACCURATERIP_TAGS)
    page = BeautifulSoup(client.get(f"/album/{_id_for(cfg, d)}").text, "html.parser")
    identity = page.select_one("#album-identity")
    assert identity is not None
    origin = identity.select_one("dd[data-field='origin']")
    assert origin.select_one("a, span").get_text(strip=True) == ("CD" if ripped else "Bandcamp ↗")
    link = identity.select_one(f'a[href="{url}"]')
    if ripped:
        assert link is None
    else:
        assert link is not None
        assert link in origin.descendants
    assert bool(page.select_one("#album-info")) is ripped


def test_album_page_has_no_additional_info_without_those_tags(client, cfg):  # noqa: F811
    d = _make_album(cfg, "Plain")
    page = BeautifulSoup(client.get(f"/album/{_id_for(cfg, d)}").text, "html.parser")
    assert page.select_one("#album-info") is None
