"""Where the files came from: download and rip evidence in their own tags (#632)."""

import pytest

from harmonist import contributions, scanner
from test.helpers import (
    ACCURATERIP_TAGS,
    AMAZON_TAGS,
    BEATPORT_TAGS,
    QOBUZ_TAGS,
    TOC_TAGS,
    write_provenance_tags,
)
from test.test_contributions import URL as BANDCAMP_URL
from test.test_contributions import release
from test.test_formats import FIXTURES
from test.test_upc_contributions import download


def assessed(folder, formats=("CD",)):
    """The album matched to a release of `formats`, with its group browsed —
    so the Possible mismatch filter has everything it needs to list it."""
    album = scanner.scan(folder.parent)[0]
    matched = release(formats)
    contributions.observe(album, matched, None)
    contributions.observe_group(album, [matched], 1)
    return contributions.assess(album)


@pytest.mark.parametrize(("ext", "fixture"), FIXTURES)
def test_a_ripped_cds_upc_is_not_download_evidence(tmp_path, ext, fixture):
    folder = download(tmp_path, ext=ext, marks={})
    write_provenance_tags(folder, ext, {**ACCURATERIP_TAGS, **TOC_TAGS})
    assessment = assessed(folder)
    assert not assessment.eligible
    assert not contributions.possible_mismatch(assessment)


def test_a_upc_alone_does_not_prove_a_download(tmp_path):
    assert not assessed(download(tmp_path, marks={})).eligible


@pytest.mark.parametrize(
    ("ext", "marks"),
    [
        (".m4a", QOBUZ_TAGS),
        (".flac", QOBUZ_TAGS),
        (".opus", QOBUZ_TAGS),
        (".mp3", AMAZON_TAGS),
        (".mp3", BEATPORT_TAGS),
    ],
)
def test_a_stores_own_tags_prove_a_download(tmp_path, ext, marks):
    assessment = assessed(download(tmp_path, ext=ext, marks=marks))
    assert assessment.eligible
    assert contributions.possible_mismatch(assessment)


def test_a_transcodes_synthesized_toc_is_not_rip_evidence(tmp_path):
    folder = download(tmp_path, marks=QOBUZ_TAGS)
    write_provenance_tags(folder, ".m4a", TOC_TAGS)
    assert assessed(folder).eligible


@pytest.mark.parametrize("marks", [QOBUZ_TAGS, {"comment": (f"Visit {BANDCAMP_URL}",)}])
def test_download_and_rip_evidence_together_prove_nothing(tmp_path, marks):
    folder = download(tmp_path, marks=marks)
    write_provenance_tags(folder, ".m4a", ACCURATERIP_TAGS)
    assert not assessed(folder).eligible
