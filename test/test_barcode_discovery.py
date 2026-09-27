"""Barcode discovery uses purchased tags; it never writes to the audio files."""

import shutil
from unittest.mock import Mock

import pytest
from mutagen.flac import FLAC
from mutagen.mp4 import MP4

from harmonist import mb_cache, mb_search, reconcile, scanner, sidecar
from harmonist.models import Sidecar
from test import test_web

cfg = test_web.cfg
client = test_web.client

BARCODE = "0801061000332"
MBID = "b1a3a871-16c2-4e8b-b775-22785decf471"
OTHER = "2bf4bf22-ddb6-41da-8870-8913bd7f3f5a"


def album(root, suffix="m4a"):
    path = root / "LFO" / "Frequencies"
    path.mkdir(parents=True)
    file = path / f"01 LFO.{suffix}"
    shutil.copy(f"test/fixtures/sine.{suffix}", file)
    tags = MP4(file) if suffix == "m4a" else FLAC(file)
    tags.clear()
    if suffix == "m4a":
        tags["©alb"] = ["Frequencies"]
        tags["aART"] = ["LFO"]
        tags["----:com.apple.iTunes:UPC"] = [BARCODE.encode()]
    else:
        tags["ALBUM"] = ["Frequencies"]
        tags["ALBUMARTIST"] = ["LFO"]
        tags["UPC"] = [BARCODE]
    tags.save()
    return path, file


def release(mbid=MBID, barcode=BARCODE):
    return {
        "id": mbid,
        "title": "Frequencies",
        "barcode": barcode,
        "artist-credit-phrase": "LFO",
        "medium-list": [
            {
                "position": "1",
                "track-count": "1",
                "track-list": [
                    {
                        "position": "1",
                        "number": "1",
                        "length": "1000",
                        "recording": {"title": "LFO"},
                    }
                ],
            }
        ],
    }


def services(monkeypatch, releases):
    search = Mock(return_value={"release-list": releases, "release-count": len(releases)})
    fetch = Mock(side_effect=lambda mbid, **kw: next(r for r in releases if r["id"] == mbid))
    monkeypatch.setattr("musicbrainzngs.search_releases", search)
    monkeypatch.setattr("harmonist.mb_cache.fetch_release", fetch)
    return search, fetch


@pytest.mark.parametrize("suffix", ["flac", "m4a"])
@pytest.mark.parametrize("media_format", ["Digital Media", "CD"])
def test_adoption_suggests_unique_barcode_without_tagging(
    tmp_path, monkeypatch, suffix, media_format
):
    path, file = album(tmp_path, suffix)
    before = file.read_bytes()
    candidate = release()
    candidate["medium-list"][0]["format"] = media_format
    search, fetch = services(monkeypatch, [candidate])
    result = reconcile.reconcile_album(path, fetch_urls=lambda _: [])
    assert result is not None
    assert result.mb_release_id is None
    assert result.mb_match_candidate.mb_release_id == MBID
    assert file.read_bytes() == before
    assert search.call_count == fetch.call_count == 1
    assert reconcile.reconcile_album(path, fetch_urls=lambda _: []) is None
    assert search.call_count == 1
    sidecar.write(path, Sidecar())  # dismissed / already adopted
    assert reconcile.reconcile_album(path, fetch_urls=lambda _: []) is None
    assert search.call_count == 1


def test_lfo_equivalent_barcodes_are_multiple_candidates(tmp_path, monkeypatch):
    path, file = album(tmp_path)
    before = file.read_bytes()
    variation = {**release(OTHER, "801061000332"), "title": "Frequencies (Remastered)"}
    search, fetch = services(monkeypatch, [release(), variation])
    result = reconcile.reconcile_album(path, fetch_urls=lambda _: [])
    assert result is not None
    assert result.mb_release_id is None
    assert result.mb_match_candidate is None
    assert file.read_bytes() == before
    assert search.call_count == 1
    assert fetch.call_count == 0
    query = search.call_args.kwargs["query"]
    assert "0801061000332" in query and "801061000332" in query


def test_search_hits_must_have_the_same_barcode(tmp_path, monkeypatch):
    path, _ = album(tmp_path)
    search, fetch = services(monkeypatch, [release(barcode="0801061000639")])
    result = reconcile.reconcile_album(path, fetch_urls=lambda _: [])
    assert result is not None and result.mb_match_candidate is None
    assert search.call_count == 1 and fetch.call_count == 0


@pytest.mark.parametrize(
    "artist,title", [("L.F.O.", "Frequencies"), ("LFO", "Frequencies (Remastered)")]
)
def test_name_variations_do_not_hide_barcode_results(tmp_path, monkeypatch, artist, title):
    path, file = album(tmp_path)
    before = file.read_bytes()
    variation = {**release(), "artist-credit-phrase": artist, "title": title}
    services(monkeypatch, [variation])
    result = reconcile.reconcile_album(path, fetch_urls=lambda _: [])
    assert result.mb_match_candidate.mb_release_id == MBID
    assert result.mb_release_id is None
    assert file.read_bytes() == before


def test_barcode_discovery_does_not_require_name_tags(tmp_path, monkeypatch):
    path, file = album(tmp_path)
    tags = MP4(file)
    del tags["aART"]
    del tags["©alb"]
    tags.save()
    services(monkeypatch, [release()])
    result = reconcile.reconcile_album(path, fetch_urls=lambda _: [])
    assert result.mb_match_candidate.mb_release_id == MBID


def test_conflicting_identifiers_skip_lookup(tmp_path, monkeypatch):
    path, file = album(tmp_path)
    tags = MP4(file)
    tags["----:com.apple.iTunes:BARCODE"] = [b"0801061000639"]
    tags.save()
    search, fetch = services(monkeypatch, [release()])
    assert reconcile.reconcile_album(path, fetch_urls=lambda _: []) is None
    assert search.call_count == fetch.call_count == 0


def test_lookup_failure_is_not_adoption(tmp_path, monkeypatch):
    path, _ = album(tmp_path)
    search, _ = services(monkeypatch, [])
    import musicbrainzngs

    search.side_effect = musicbrainzngs.NetworkError("offline")
    with pytest.raises(mb_search.MBSearchError, match="offline"):
        reconcile.reconcile_album(path, fetch_urls=lambda _: [])
    assert sidecar.read(path) is None


def test_barcode_recheck_lists_lfo_choices(client, cfg, monkeypatch):
    path, file = album(cfg.paths.music_dir)
    sidecar.write(path, Sidecar())
    aid = scanner.scan(cfg.paths.music_dir)[0].id
    before = file.read_bytes()
    services(monkeypatch, [release(), release(OTHER, "801061000332")])
    response = client.post(f"/manual/{aid}/barcode")
    assert response.status_code == 200
    assert MBID in response.text and OTHER in response.text
    assert 'name="review_only" value="true"' in response.text
    assert file.read_bytes() == before
    assert sidecar.read(path).mb_match_candidate is None


def test_barcode_recheck_suggests_and_uses_fresh_release(client, cfg, monkeypatch):
    path, file = album(cfg.paths.music_dir)
    sidecar.write(path, Sidecar())
    aid = scanner.scan(cfg.paths.music_dir)[0].id
    before = file.read_bytes()
    _, fetch = services(monkeypatch, [release()])
    response = client.post(f"/manual/{aid}/barcode")
    assert response.status_code == 200
    assert sidecar.read(path).mb_match_candidate.mb_release_id == MBID
    assert fetch.call_args.kwargs["max_age"] == mb_cache.FRESH
    assert file.read_bytes() == before


def test_truncated_results_never_claim_uniqueness(tmp_path, monkeypatch):
    path, _ = album(tmp_path)
    search, fetch = services(monkeypatch, [release()])
    search.return_value["release-count"] = 101
    result = reconcile.reconcile_album(path, fetch_urls=lambda _: [])
    assert result is not None and result.mb_match_candidate is None
    assert fetch.call_count == 0


@pytest.mark.parametrize("suffix", ["flac", "m4a"])
@pytest.mark.parametrize("second_code", [None, "0801061000639", "0801061000333"])
def test_every_file_must_supply_consistent_valid_evidence(
    tmp_path, monkeypatch, suffix, second_code
):
    path, file = album(tmp_path, suffix)
    second = path / f"02 Other.{suffix}"
    shutil.copy(file, second)
    tags = FLAC(second) if suffix == "flac" else MP4(second)
    key = "UPC" if suffix == "flac" else "----:com.apple.iTunes:UPC"
    if second_code is None:
        del tags[key]
    else:
        tags[key] = [second_code if suffix == "flac" else second_code.encode()]
    tags.save()
    search, _ = services(monkeypatch, [release()])
    assert reconcile.reconcile_album(path, fetch_urls=lambda _: []) is None
    assert search.call_count == 0
    assert scanner.scan(tmp_path)[0].barcode is None


def test_tasks_starts_barcode_adoption(client, cfg, monkeypatch):
    album(cfg.paths.music_dir)
    start = Mock()
    monkeypatch.setattr(client.app.state.reconcile_runner, "start", start)
    assert client.get("/tasks").status_code == 200
    start.assert_called_once()


def test_barcode_selection_requires_review_even_with_exact_lengths(client, cfg, monkeypatch):
    path, file = album(cfg.paths.music_dir)
    sidecar.write(path, Sidecar())
    aid = scanner.scan(cfg.paths.music_dir)[0].id
    before = file.read_bytes()
    exact = release()
    exact["medium-list"][0]["track-list"][0]["length"] = str(round(MP4(file).info.length * 1000))
    services(monkeypatch, [exact])
    response = client.post(f"/manual/{aid}/assign", data={"mbid": MBID, "review_only": "true"})
    assert response.status_code == 200
    assert sidecar.read(path).mb_match_candidate.confidence == "exact"
    assert sidecar.read(path).mb_release_id is None
    assert file.read_bytes() == before


def test_recheck_failure_preserves_existing_suggestion(client, cfg, monkeypatch):
    path, _ = album(cfg.paths.music_dir)
    _, fetch = services(monkeypatch, [release()])
    reconcile.reconcile_album(path, fetch_urls=lambda _: [])
    aid = scanner.scan(cfg.paths.music_dir)[0].id
    before = (path / ".harmonist.json").read_bytes()
    from harmonist.mb_lookup import MBError

    fetch.side_effect = MBError("offline")
    response = client.post(f"/manual/{aid}/barcode")
    assert "Barcode lookup failed" in response.text
    assert (path / ".harmonist.json").read_bytes() == before


def test_empty_lookup_remains_retryable_and_offers_harmony(client, cfg, monkeypatch):
    path, _ = album(cfg.paths.music_dir)
    search, fetch = services(monkeypatch, [])
    reconcile.reconcile_album(path, fetch_urls=lambda _: [])
    aid = scanner.scan(cfg.paths.music_dir)[0].id
    page = client.get(f"/album/{aid}")
    # Qobuz's Harmony provider searches the literal spelling; padding this UPC
    # to GTIN-14 loses the Qobuz result even though MB comparison needs padding.
    assert 'value="barcode"' in page.text
    response = client.post(f"/manual/{aid}/barcode")
    assert "No matches" in response.text
    assert "gtin=0801061000332" in response.text
    assert "Open in Harmony" in response.text
    assert search.call_count == 2 and fetch.call_count == 0


def test_search_and_fetched_release_must_agree(tmp_path, monkeypatch):
    path, _ = album(tmp_path)
    _, fetch = services(monkeypatch, [release()])
    fetch.side_effect = None
    fetch.return_value = release(barcode="0801061000639")
    with pytest.raises(mb_search.MBSearchError, match="changed"):
        reconcile.reconcile_album(path, fetch_urls=lambda _: [])
    assert sidecar.read(path) is None


def test_store_url_search_suggests_without_tagging(client, cfg, monkeypatch):
    path, file = album(cfg.paths.music_dir)
    sidecar.write(path, Sidecar(store_url="https://lfo.bandcamp.com/album/frequencies"))
    aid = scanner.scan(cfg.paths.music_dir)[0].id
    before = file.read_bytes()
    _, fetch = services(monkeypatch, [release()])
    lookup = Mock(return_value=([{"id": MBID}], 1))
    monkeypatch.setattr("harmonist.mb_lookup.candidate_summaries_for_url", lookup)
    response = client.post(f"/manual/{aid}/candidates", data={"suggest": "true"})
    assert response.status_code == 200
    assert sidecar.read(path).mb_match_candidate.mb_release_id == MBID
    assert sidecar.read(path).mb_release_id is None
    assert file.read_bytes() == before
    assert fetch.call_args.kwargs["max_age"] == mb_cache.FRESH
    assert lookup.call_count == fetch.call_count == 1
