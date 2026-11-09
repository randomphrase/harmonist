"""A disc ID computed from the tags' CD table of contents finds releases (#984)."""

import json
import shutil
from unittest.mock import Mock

import musicbrainzngs
import mutagen
import pytest
from bs4 import BeautifulSoup
from mutagen.id3 import COMM, ID3
from mutagen.mp4 import MP4

from harmonist import disc_ids, formats, mb_search, scanner, sidecar
from harmonist.formats.types import ScanFields
from harmonist.models import FoundBy, Sidecar
from harmonist.web import main
from test import test_web

cfg = test_web.cfg
client = test_web.client

# LCD Soundsystem's American Dream, as XLD tagged it, and the disc ID
# MusicBrainz lists that disc under.
AMERICAN_DREAM = (
    "8E101B0A+309388+10+150+26377+56628+81557+103873+145291+171342+202750+230259+254950"
)
AMERICAN_DREAM_ID = "GXLUXbKE3ZzkH0OMuyTr84b41ro-"

ONE_TRACK = "02000F01+1000+1+150"
TWO_TRACKS = "03000F02+2000+2+150+1000"
THREE_TRACKS = "04000F03+3000+3+150+1000+2000"
MBID = "6b072dd5-c6fd-4140-a915-aaedf2c85115"
ATOM = "----:com.apple.iTunes:iTunes_CDDB_1"


def track(toc, disc=None, unreadable=False):
    return ScanFields(None, None, None, None, cd_toc=toc, disc_num=disc, unreadable=unreadable)


def id_of(toc):
    parsed = disc_ids.parse(toc)
    assert parsed is not None
    return disc_ids.disc_id(parsed)


def album(root, *, toc=ONE_TRACK, upc=None):
    path = root / "LCD Soundsystem" / "American Dream"
    path.mkdir(parents=True)
    file = path / "01 Oh Baby.m4a"
    shutil.copy("test/fixtures/sine.m4a", file)
    tags = MP4(file)
    tags.clear()
    tags["©alb"] = ["American Dream"]
    tags["aART"] = ["LCD Soundsystem"]
    if toc:
        tags[ATOM] = [toc.encode()]
    if upc:
        tags["----:com.apple.iTunes:UPC"] = [upc.encode()]
    tags.save()
    return path, file


def release(mbid=MBID):
    return {
        "id": mbid,
        "title": "American Dream",
        "artist-credit-phrase": "LCD Soundsystem",
        "medium-list": [{"position": "1", "format": "CD", "track-count": 1}],
    }


def lookup(monkeypatch, releases=(), side_effect=None):
    releases = list(releases)
    mock = Mock(
        side_effect=side_effect,
        return_value={
            "disc": {
                "id": AMERICAN_DREAM_ID,
                "release-list": releases,
                "release-count": len(releases),
            }
        },
    )
    monkeypatch.setattr("musicbrainzngs.get_releases_by_discid", mock)
    return mock


def not_found():
    return musicbrainzngs.ResponseError(cause=Mock(code=404))


# ---------------------------------------------------------------- the disc ID


def test_the_disc_id_is_the_one_musicbrainz_lists_the_disc_under():
    assert id_of(AMERICAN_DREAM) == AMERICAN_DREAM_ID


@pytest.mark.parametrize(
    "value",
    [
        "",
        "garbage",
        "+1000+1+150",  # no CDDB id
        "02000F01+x+1+150",
        "02000F01+1000+x+150",
        "02000F01+1000+1+x",
        "02000F01+1000+0",
        "02000F01+1000+2+150",  # fewer offsets than the count
        "02000F01+1000+1+150+600",  # more offsets than the count
        "02000F01+-1000+1+150",
        "02000F01+1000+1+-150",
        "03000F02+2000+2+1000+1000",  # offsets not increasing
        "03000F02+2000+2+1000+150",
        "03000F02+1000+2+150+1000",  # lead-out not after the last track
        "+".join(["02000F01", "9999", "100", *map(str, range(150, 250))]),  # a CD has 99 at most
    ],
)
def test_a_table_of_contents_that_cannot_describe_a_disc_is_rejected(value):
    assert disc_ids.parse(value) is None


def test_whitespace_around_the_value_is_not_part_of_it():
    assert disc_ids.parse(f" {ONE_TRACK} ") == disc_ids.parse(ONE_TRACK)


# ------------------------------------------------------------- from the files


def test_a_disc_whose_files_agree_gives_its_disc_id():
    assert disc_ids.evidence([track(AMERICAN_DREAM)] * 10) == AMERICAN_DREAM_ID


@pytest.mark.parametrize(
    "tracks",
    [
        pytest.param([], id="no-files"),
        pytest.param([track(TWO_TRACKS), track(None)], id="a-file-without-the-tag"),
        pytest.param(
            [track(TWO_TRACKS), track("03000F02+2000+2+150+1001")], id="files-that-disagree"
        ),
        pytest.param([track(TWO_TRACKS)], id="fewer-files-than-tracks"),
        pytest.param([track(ONE_TRACK), track(ONE_TRACK)], id="more-files-than-tracks"),
        pytest.param([track("garbage"), track("garbage")], id="malformed"),
        pytest.param([track(ONE_TRACK, unreadable=True)], id="an-unreadable-file"),
    ],
)
def test_files_that_do_not_describe_one_whole_disc_offer_nothing(tracks):
    assert disc_ids.evidence(tracks) is None


def test_a_multi_disc_album_looks_up_its_first_disc_whatever_the_file_order():
    tracks = [track(THREE_TRACKS, disc=2)] * 3 + [track(TWO_TRACKS, disc=1)] * 2
    assert disc_ids.evidence(tracks) == id_of(TWO_TRACKS)


def test_a_bad_later_disc_rules_out_the_whole_album():
    tracks = [track(TWO_TRACKS, disc=1)] * 2 + [track("garbage", disc=2)] * 3
    assert disc_ids.evidence(tracks) is None


@pytest.mark.parametrize("suffix", ["m4a", "flac", "opus", "mp3"])
def test_every_tag_system_reads_the_table_of_contents(tmp_path, suffix):
    file = tmp_path / f"track.{suffix}"
    shutil.copy(f"test/fixtures/sine.{suffix}", file)
    if suffix == "m4a":
        tags = MP4(file)
        tags[ATOM] = [ONE_TRACK.encode()]
        tags.save()
    elif suffix == "mp3":
        id3 = ID3()
        id3.add(COMM(encoding=3, lang="eng", desc="iTunes_CDDB_1", text=[ONE_TRACK]))
        id3.save(file)
    else:
        vorbis = mutagen.File(file)
        vorbis["ITUNES_CDDB_1"] = [ONE_TRACK]
        vorbis.save()

    assert formats.read_scan_fields(file).cd_toc == ONE_TRACK


def test_the_scan_puts_the_disc_id_on_the_album(tmp_path):
    album(tmp_path)
    assert scanner.scan(tmp_path)[0].disc_id == id_of(ONE_TRACK)


# ------------------------------------------------------------- the request


def test_the_lookup_is_one_exact_request(monkeypatch):
    mock = lookup(monkeypatch, [release()])

    results, total = mb_search.search_disc_id(AMERICAN_DREAM_ID)

    assert [r["id"] for r in results] == [MBID]
    assert results[0]["artist"] == "LCD Soundsystem" and results[0]["media"] == "CD"
    assert total == 1
    # Passing a table of contents would turn this into MusicBrainz's fuzzy
    # match, which #984 decided against.
    mock.assert_called_once_with(
        AMERICAN_DREAM_ID, includes=["artist-credits", "labels"], cdstubs=False
    )


def test_a_disc_nobody_has_submitted_is_an_empty_answer(monkeypatch):
    lookup(monkeypatch, side_effect=not_found())
    assert mb_search.search_disc_id(AMERICAN_DREAM_ID) == ([], 0)


@pytest.mark.parametrize(
    "failure",
    [
        musicbrainzngs.ResponseError(cause=Mock(code=503)),
        musicbrainzngs.NetworkError("offline"),
    ],
)
def test_a_failed_lookup_is_not_an_empty_answer(monkeypatch, failure):
    lookup(monkeypatch, side_effect=failure)
    with pytest.raises(mb_search.MBSearchError):
        mb_search.search_disc_id(AMERICAN_DREAM_ID)


# --------------------------------------------------------------- the route


def test_the_search_lists_its_releases_and_stores_nothing(client, cfg, monkeypatch):
    path, file = album(cfg.paths.music_dir)
    sidecar.write(path, Sidecar())
    aid = scanner.scan(cfg.paths.music_dir)[0].id
    before = file.read_bytes()
    mock = lookup(monkeypatch, [release()])

    response = client.post(f"/manual/{aid}/disc-id")

    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    row = soup.select_one('[role="listitem"]')
    assert row is not None and "Disc ID matches" in row.get_text()
    review = soup.select_one(f'button[hx-get^="/review/{aid}?release="]')
    assert review is not None and f"found_by={FoundBy.DISC_ID}" in review["hx-get"]
    assert response.headers["HX-Retarget"] == f"#mbid-results-{aid}"
    assert mock.call_count == 1
    assert file.read_bytes() == before
    assert sidecar.read(path).mb_match_candidate is None


def test_an_automatic_lookup_that_finds_nothing_hands_over_to_the_name_search(
    client, cfg, monkeypatch
):
    path, _ = album(cfg.paths.music_dir)
    sidecar.write(path, Sidecar())
    aid = scanner.scan(cfg.paths.music_dir)[0].id
    lookup(monkeypatch, side_effect=not_found())

    response = client.post(f"/manual/{aid}/disc-id", data={"automatic": "true"})

    assert json.loads(response.headers["HX-Trigger-After-Swap"]) == {
        "disc-id-empty": {"target": f"#mbid-results-{aid}"}
    }


def test_an_automatic_lookup_that_finds_a_release_stays_put(client, cfg, monkeypatch):
    path, _ = album(cfg.paths.music_dir)
    sidecar.write(path, Sidecar())
    aid = scanner.scan(cfg.paths.music_dir)[0].id
    lookup(monkeypatch, [release()])

    response = client.post(f"/manual/{aid}/disc-id", data={"automatic": "true"})

    assert "HX-Trigger-After-Swap" not in response.headers


def test_a_search_the_user_asked_for_says_it_found_nothing(client, cfg, monkeypatch):
    path, _ = album(cfg.paths.music_dir)
    sidecar.write(path, Sidecar())
    aid = scanner.scan(cfg.paths.music_dir)[0].id
    lookup(monkeypatch, side_effect=not_found())

    response = client.post(f"/manual/{aid}/disc-id")

    assert "No matches" in response.text
    assert "HX-Trigger-After-Swap" not in response.headers


def test_a_failed_lookup_is_reported_and_writes_nothing(client, cfg, monkeypatch):
    path, _ = album(cfg.paths.music_dir)
    sidecar.write(path, Sidecar())
    aid = scanner.scan(cfg.paths.music_dir)[0].id
    before = (path / ".harmonist.json").read_bytes()
    lookup(monkeypatch, side_effect=musicbrainzngs.NetworkError("offline"))

    response = client.post(f"/manual/{aid}/disc-id", data={"automatic": "true"})

    assert "Disc ID lookup failed" in response.text
    assert response.headers["HX-Retarget"] == f"#mbid-results-{aid}"
    # Not "found nothing": handing over would hide the failure.
    assert "HX-Trigger-After-Swap" not in response.headers
    assert (path / ".harmonist.json").read_bytes() == before


@pytest.mark.parametrize(
    ("toc", "state"),
    [
        pytest.param(None, Sidecar(), id="no-table-of-contents"),
        pytest.param(ONE_TRACK, Sidecar(mb_release_id=MBID), id="already-matched"),
    ],
)
def test_the_lookup_needs_an_unmatched_album_with_a_disc_id(client, cfg, monkeypatch, toc, state):
    path, _ = album(cfg.paths.music_dir, toc=toc)
    sidecar.write(path, state)
    aid = scanner.scan(cfg.paths.music_dir)[0].id
    mock = lookup(monkeypatch, [release()])

    response = client.post(f"/manual/{aid}/disc-id")

    assert "Disc ID lookup unavailable" in response.text
    assert mock.call_count == 0


# ---------------------------------------------------------------- the choice


def _modes(client, aid):
    soup = BeautifulSoup(client.get(f"/album/{aid}").text, "html.parser")
    radios = soup.select(f'input[type="radio"][name="find-{aid}"]')
    return {r["value"]: r.has_attr("checked") for r in radios}, soup


STORE_URL = "https://artist.bandcamp.com/album/record"


@pytest.mark.parametrize(
    ("toc", "upc", "store_url", "expected"),
    [
        pytest.param(ONE_TRACK, None, None, "discid", id="nothing-stronger"),
        pytest.param(ONE_TRACK, "0801061000332", None, "barcode", id="a-barcode-outranks-it"),
        pytest.param(ONE_TRACK, None, STORE_URL, "url", id="a-store-url-outranks-it"),
        pytest.param(None, None, None, "name", id="no-table-of-contents"),
    ],
)
def test_the_default_mode_is_the_strongest_evidence_the_album_has(
    client, cfg, toc, upc, store_url, expected
):
    path, _ = album(cfg.paths.music_dir, toc=toc, upc=upc)
    sidecar.write(path, Sidecar(store_url=store_url))
    aid = scanner.scan(cfg.paths.music_dir)[0].id

    modes, _ = _modes(client, aid)

    assert [mode for mode, checked in modes.items() if checked] == [expected]
    assert ("discid" in modes) is bool(toc)


@pytest.mark.parametrize("tagged", [False, True])
def test_only_an_album_with_nothing_naming_its_release_looks_itself_up(client, cfg, tagged):
    """As for the barcode (#671): files tagged with an MBID are reconcile's to
    adopt, and a lookup fired on render would race it."""
    path, file = album(cfg.paths.music_dir)
    sidecar.write(path, Sidecar())
    if tagged:
        tags = MP4(file)
        tags["----:com.apple.iTunes:MusicBrainz Album Id"] = [MBID.encode()]
        tags.save()
    aid = scanner.scan(cfg.paths.music_dir)[0].id

    _, soup = _modes(client, aid)

    form = soup.select_one(f'form[hx-post="/manual/{aid}/disc-id"]')
    assert form is not None
    assert ("load" in form["hx-trigger"]) is not tagged
    assert "submit" in form["hx-trigger"]
    assert (form.select_one('input[name="automatic"]') is not None) is not tagged


def test_the_inbox_offers_the_search_and_waits_to_be_asked(client, cfg):
    """The Inbox draws every unmatched album at once, so a lookup per card would
    spend a rate-limited request per untagged rip on each load."""
    path, _ = album(cfg.paths.music_dir)
    sidecar.write(path, Sidecar())
    aid = scanner.scan(cfg.paths.music_dir)[0].id

    soup = BeautifulSoup(client.get("/tasks").text, "html.parser")

    radio = soup.select_one(f'input[type="radio"][name="find-{aid}"][value="discid"]')
    assert radio is not None and radio.has_attr("checked")
    form = soup.select_one(f'form[hx-post="/manual/{aid}/disc-id"]')
    assert form is not None and "load" not in form["hx-trigger"]
    assert form.select_one('input[name="automatic"]') is None


def test_a_mode_that_is_not_the_default_does_not_look_itself_up(client, cfg):
    path, _ = album(cfg.paths.music_dir, upc="0801061000332")
    sidecar.write(path, Sidecar())
    aid = scanner.scan(cfg.paths.music_dir)[0].id

    _, soup = _modes(client, aid)

    form = soup.select_one(f'form[hx-post="/manual/{aid}/disc-id"]')
    assert form is not None and "load" not in form["hx-trigger"]
    assert form.select_one('input[name="automatic"]') is None


def test_every_way_of_finding_a_release_has_its_history_wording():
    assert set(main._FOUND_BY) == set(FoundBy)
