"""Original download UPCs survive MB tagging and inform contribution review."""

import shutil
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import musicbrainzngs
import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from mutagen import File
from mutagen.id3 import TXXX

from harmonist import activity_store, contributions, formats, mb_cache, mb_lookup, scanner, sidecar
from harmonist.config import Config, PathsConfig
from harmonist.models import Sidecar
from harmonist.web.main import _library_page_vars, create_app
from test.test_contributions import MBID, release
from test.test_formats import FIXTURES, _tagset
from test.test_gardener import _release
from test.test_web import _confirmation_fields

UPC = "0801061000332"
OTHER_UPC = "0801061000639"


def download(root, upcs=(UPC,), ext=".m4a", name="Download"):
    folder = root / name
    folder.mkdir(parents=True)
    for i, values in enumerate(upcs, 1):
        path = folder / f"{i:02d}{ext}"
        shutil.copy(Path(__file__).parent / "fixtures" / f"sine{ext}", path)
        formats.write_tags(
            path,
            _tagset(mb_album_id=MBID, barcode=OTHER_UPC, track_num=i, track_total=len(upcs)),
            None,
        )
        tags = File(path)
        values = (values,) if isinstance(values, str) else values
        if values:
            if ext == ".m4a":
                tags["----:com.apple.iTunes:UPC"] = [v.encode() for v in values]
            elif ext == ".mp3":
                tags.tags.add(TXXX(encoding=3, desc="UPC", text=list(values)))
            else:
                tags["UPC"] = list(values)
            tags.save()
    sidecar.write(folder, Sidecar(mb_release_id=MBID))
    return folder


def remember(payload):
    activity_store.store_release(MBID, mb_cache._key(mb_lookup.RELEASE_INCLUDES), payload)


@pytest.mark.parametrize("ext,fixture", FIXTURES)
def test_original_upc_qualifies_even_when_owned_barcode_disagrees(
    tmp_path, monkeypatch, ext, fixture
):
    activity_store.init(tmp_path / "activity.db")
    download(tmp_path / "music", (UPC, "801061000332"), ext)
    remember(release(("CD",)))
    fetch = Mock(side_effect=AssertionError("scan and filter must use stored observations"))
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    albums = scanner.scan(tmp_path / "music")
    assert contributions.assess(albums[0]).media_mismatch is True
    assert contributions.assess(albums[0]).store_url is None
    assert _library_page_vars(albums, 1, 30, filter_="mb-contributions")["rows"] == albums
    assert fetch.call_count == 0


@pytest.mark.parametrize(
    "upcs", [(None,), (UPC, None), (UPC, OTHER_UPC), ("bad",), ((UPC, OTHER_UPC),)]
)
def test_incomplete_or_conflicting_source_upcs_do_not_prove_download(tmp_path, upcs):
    download(tmp_path, upcs)
    a = scanner.scan(tmp_path)[0]
    contributions.observe(a, release(("CD",)), None)
    assert not contributions.assess(a).eligible


def test_malformed_upc_value_cannot_be_hidden_by_a_readable_value(tmp_path):
    folder = download(tmp_path)
    path = folder / "01.m4a"
    tags = File(path)
    tags["----:com.apple.iTunes:UPC"] = [UPC.encode(), b"\xff"]
    tags.save()
    a = scanner.scan(tmp_path)[0]
    contributions.observe(a, release(("CD",)), None)
    assert not contributions.assess(a).eligible


@pytest.mark.parametrize("second", [None, UPC, OTHER_UPC, "unreadable"])
def test_source_evidence_covers_all_parts_but_not_other_copies(tmp_path, second):
    first = download(tmp_path, name="Disc 1") / "01.m4a"
    other = (
        download(tmp_path, (None if second == "unreadable" else second,), name="Disc 2") / "01.m4a"
    )
    for disc, path in enumerate((first, other), 1):
        fields = formats.read_owned(path)
        fields["disc_num"] = disc
        formats.write_owned(path, fields)
    if second == "unreadable":
        other.write_bytes(b"not audio")
    albums = scanner.scan(tmp_path)
    # An unreadable part may prevent grouping; no copy without evidence may
    # borrow the first part's UPC. A readable merged album needs every part.
    for a in albums:
        contributions.observe(a, release(("CD",)), None)
        expected = second == UPC or (a.path == first.parent and len(albums) == 2)
        assert contributions.assess(a).eligible is expected


@pytest.fixture
def upc_library(tmp_path):
    cfg = Config(paths=PathsConfig(music_dir=tmp_path / "music", config_dir=tmp_path / "config"))
    cfg.paths.config_dir.mkdir()
    activity_store.init(tmp_path / "activity.db")
    download(cfg.paths.music_dir)
    client = TestClient(create_app(cfg), headers={"HX-Request": "true"})
    remember(release(("CD",)))
    return client, cfg.paths.music_dir


@pytest.mark.parametrize("kind", ["unique", "ambiguous", "count", "unknown", "truncated", "empty"])
def test_upc_siblings_use_one_fresh_scoped_browse_and_review(upc_library, monkeypatch, kind):
    client, root = upc_library
    first = release(mbid="digital")
    first["barcode"] = "801061000332"
    first["medium-list"][0]["track-count"] = 2 if kind == "count" else 1
    second = release(mbid="other-digital")
    second["barcode"] = UPC if kind == "ambiguous" else OTHER_UPC
    second["medium-list"][0]["track-count"] = 1
    releases = [] if kind == "empty" else [second, first]
    if kind == "unknown":
        releases.append(release(("",), mbid="unknown"))
    browse = Mock(
        return_value={
            "release-list": releases,
            "release-count": 101 if kind == "truncated" else len(releases),
        }
    )
    monkeypatch.setattr(musicbrainzngs, "browse_releases", browse)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    for n in (1, 2):
        page = BeautifulSoup(
            client.get(f"/library/{MBID}/contributions/editions").text, "html.parser"
        )
        suggestion = page.select_one('[role="listitem"][aria-label="Suggested digital release"]')
        assert bool(suggestion) is (kind == "unique")
        if suggestion:
            assert "Barcode matches" in suggestion.text
            button = suggestion.select_one("button")
            assert button is not None and "replacement=" in button["hx-get"]
        if kind == "empty":
            harmony = page.select_one('a[title="Add release with Harmony"]')
            assert harmony is not None and "gtin=" + UPC in harmony["href"]
        else:
            assert page.select_one('a[title="Add release with Harmony"]') is None
            assert "Possible better MB match found" in page.text
            assert "Barcode matches" in page.select_one('[role="listitem"]').text
        assert browse.call_count == n
        assert browse.call_args.kwargs["release_group"] == release()["release-group"]["id"]
    assert before == {p: p.read_bytes() for p in before}


@pytest.mark.parametrize(
    "value,wording",
    [
        (None, "Barcode missing from MusicBrainz"),
        ("", "lists this release as having no barcode"),
        (OTHER_UPC, "Barcode differs from MusicBrainz"),
        ("801061000332", None),
    ],
)
def test_digital_contributions_distinguish_missing_free_different_and_equal(
    upc_library, monkeypatch, value, wording
):
    client, _ = upc_library
    payload = release()
    if value is not None:
        payload["barcode"] = value
    remember(payload)
    page = BeautifulSoup(client.get(f"/album/{MBID}").text, "html.parser")
    panel = page.select_one(f"#album-contributions-{MBID}")
    if wording:
        assert panel.select_one('a[href$="/edit"]') is None
        monkeypatch.setattr(
            musicbrainzngs,
            "browse_releases",
            Mock(return_value={"release-list": [payload], "release-count": 1}),
        )
        results = BeautifulSoup(
            client.get(f"/library/{MBID}/contributions/editions").text, "html.parser"
        )
        assert wording in results.text
        value_input = results.select_one('input[aria-label="Original UPC to copy"]')
        assert value_input is not None and value_input["value"] == UPC
        assert "UPC tag" in results.text
        assert results.select_one(f'a[href="https://musicbrainz.org/release/{MBID}/edit"]')
    else:
        assert panel.select_one("section") is None


@pytest.mark.parametrize("match", ["url", "barcode", "neither"])
def test_missing_url_defers_all_edit_prompts_until_siblings_checked(
    upc_library, monkeypatch, match
):
    from dataclasses import replace

    from test.test_contributions import URL

    client, root = upc_library
    folder = root / "Download"
    sc = sidecar.read(folder)
    assert sc is not None
    sidecar.write(folder, replace(sc, store_url=URL, bandcamp_downloaded=True))
    remember(release())
    initial = BeautifulSoup(client.get(f"/album/{MBID}").text, "html.parser")
    panel = initial.select_one(f"#album-contributions-{MBID}")
    assert panel.select_one('a[href$="/edit"]') is None
    sibling = release(urls=(URL,) if match == "url" else (), mbid="sibling")
    sibling["barcode"] = UPC if match == "barcode" else OTHER_UPC
    sibling["medium-list"][0]["track-count"] = 1
    monkeypatch.setattr(
        musicbrainzngs,
        "browse_releases",
        Mock(return_value={"release-list": [sibling], "release-count": 1}),
    )
    results = BeautifulSoup(
        client.get(f"/library/{MBID}/contributions/editions").text, "html.parser"
    )
    assert bool(results.select('a[href$="/edit"]')) is (match == "neither")
    assert len(results.select("p > strong")) == 1
    assert "Barcode missing from MusicBrainz" not in results.text
    assert ("Store URL missing from this release" in results.text) is (match == "neither")


@pytest.mark.parametrize("barcode", [None, "", OTHER_UPC])
@pytest.mark.parametrize("kind", ["linked", "absent", "unknown", "truncated", "failure"])
def test_barcode_findings_wait_for_complete_sibling_check(upc_library, monkeypatch, barcode, kind):
    client, root = upc_library
    current = release()
    if barcode is not None:
        current["barcode"] = barcode
    remember(current)
    sibling = release(("",) if kind == "unknown" else ("Digital Media",), mbid="sibling")
    sibling["barcode"] = UPC if kind == "linked" else OTHER_UPC
    sibling["medium-list"][0]["track-count"] = 1
    browse = Mock(
        return_value={
            "release-list": [current, sibling],
            "release-count": 101 if kind == "truncated" else 2,
        }
    )
    if kind == "failure":
        browse.side_effect = musicbrainzngs.NetworkError("offline")
    monkeypatch.setattr(musicbrainzngs, "browse_releases", browse)
    fetch = Mock(side_effect=AssertionError("the current release is already stored"))
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    for path in (f"/album/{MBID}", f"/library/{MBID}/compare"):
        page = BeautifulSoup(client.get(path).text, "html.parser")
        panel = page.select_one(f"#album-contributions-{MBID}")
        assert panel.select_one('a[href$="/edit"]') is None
        assert panel.select("p > strong") == []
        if path.endswith("/compare"):
            assert len(panel.select(f'[hx-get="/library/{MBID}/contributions/editions"]')) == 1
    assert browse.call_count == 0
    results = BeautifulSoup(
        client.get(f"/library/{MBID}/contributions/editions").text, "html.parser"
    )
    assert len(results.select("p > strong")) <= 1
    editor = results.select_one(f'a[href="https://musicbrainz.org/release/{MBID}/edit"]')
    assert bool(editor) is (kind == "absent")
    if kind == "linked":
        assert "Possible better MB match found" in results.text
        assert "Barcode matches" in results.select_one('[role="listitem"]').text
    elif kind == "absent":
        original_upc = results.select_one('input[aria-label="Original UPC to copy"]')
        assert original_upc is not None and original_upc["value"] == UPC
        assert ("Add barcode" if barcode is None else "Review barcode") in editor.text
    elif kind in {"unknown", "truncated"}:
        assert "search is incomplete" in results.text
    else:
        assert results.select_one('[role="alert"]') is not None
    if kind != "absent":
        assert results.select_one('a[title="Add release with Harmony"]') is None
    assert browse.call_count == 1
    assert fetch.call_count == 0
    assert before == {p: p.read_bytes() for p in before}


def test_reread_clears_barcode_finding_without_touching_files(upc_library, monkeypatch):
    client, root = upc_library
    payload = _release(mbid=MBID)
    remember(payload)
    initial = BeautifulSoup(client.get(f"/album/{MBID}").text, "html.parser")
    assert initial.select_one(f"#album-contributions-{MBID} section") is not None
    payload["barcode"] = UPC
    fetch = Mock(return_value=payload)
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    page = BeautifulSoup(client.get(f"/library/{MBID}/compare?reread=1").text, "html.parser")
    assert page.select_one(f"#album-contributions-{MBID} section") is None
    assert fetch.call_count == 1
    assert not contributions.assess(scanner.scan(root)[0]).has_findings
    assert before == {p: p.read_bytes() for p in before}


def test_contributions_reveal_one_finding_after_each_resolution(upc_library, monkeypatch):
    from dataclasses import replace

    from test.test_contributions import URL

    client, root = upc_library
    folder = root / "Download"
    sc = sidecar.read(folder)
    assert sc is not None
    sidecar.write(folder, replace(sc, store_url=URL, bandcamp_downloaded=True))
    browse = Mock()
    monkeypatch.setattr(musicbrainzngs, "browse_releases", browse)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    stages = [
        ("CD", OTHER_UPC, (), "Possible media mismatch", None),
        ("Digital Media", OTHER_UPC, (), "Barcode differs from MusicBrainz", "Review barcode"),
        ("Digital Media", None, (), "Store URL missing from this release", "Edit store link"),
        ("Digital Media", None, (URL,), "Barcode missing from MusicBrainz", "Add barcode"),
    ]
    for n, (media, barcode, urls, finding, action) in enumerate(stages, 1):
        payload = release((media,), urls=urls)
        if barcode is not None:
            payload["barcode"] = barcode
        remember(payload)
        browse.return_value = {"release-list": [payload], "release-count": 1}
        results = BeautifulSoup(
            client.get(f"/library/{MBID}/contributions/editions").text, "html.parser"
        )
        assert [p.text.strip().rstrip(".") for p in results.select("p > strong")] == [finding]
        editors = results.select('a[href$="/edit"]')
        assert len(editors) == (1 if action else 0)
        if action:
            assert action in editors[0].text
        assert browse.call_count == n
    payload["barcode"] = UPC
    remember(payload)
    page = BeautifulSoup(client.get(f"/album/{MBID}").text, "html.parser")
    assert page.select_one(f"#album-contributions-{MBID} section") is None
    assert browse.call_count == len(stages)
    assert before == {p: p.read_bytes() for p in before}


def test_reviewed_replacement_and_undo_preserve_original_upc(upc_library, monkeypatch):
    client, root = upc_library
    selected = _release(mbid="digital")  # no MB barcode
    fetch = Mock(return_value=selected)
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    query = f"replacement={MBID}:digital"
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    editor = client.get(f"/assignments/{MBID}?{query}&cancel=true&on_album_page=true")
    fields = _confirmation_fields(editor.text)
    assert fields["candidate_mbid"] == "digital"
    assert before == {p: p.read_bytes() for p in before}
    assert fetch.call_count == 1
    monkeypatch.setattr(mb_cache, "_ttl", timedelta(0))
    assert mb_cache.due("digital")
    fetch.side_effect = AssertionError("reviewed writes and undo must not fetch")
    applied = client.post(f"/confirm/{MBID}/accept?{query}", data=fields)
    assert "confirmation-applied" in applied.headers.get("HX-Trigger", "")
    path = root / "Download" / "01.m4a"
    assert formats.read_owned(path)["barcode"] is None
    assert formats.read_scan_fields(path).source_upcs == (UPC,)
    page = BeautifulSoup(client.get("/album/digital").text, "html.parser")
    assert page.select_one("#contribution-editions-digital") is not None
    after = {p: p.read_bytes() for p in before}
    repeated = client.post(f"/confirm/{MBID}/accept?{query}", data=fields)
    assert "confirmation-applied" not in repeated.headers.get("HX-Trigger", "")
    assert after == {p: p.read_bytes() for p in before}
    anchor = next(e.id for e in activity_store.album_history("digital") if e.message == "Tagged")
    restored = client.post("/tags/restore/digital", data={"event_id": anchor})
    assert "Tags put back" in restored.text
    assert formats.read_owned(path)["barcode"] == OTHER_UPC
    assert formats.read_scan_fields(path).source_upcs == (UPC,)
    assert fetch.call_count == 1
