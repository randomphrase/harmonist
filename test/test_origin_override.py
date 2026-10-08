"""An owner's knowledge wins over inherited provenance tags (#694)."""

import json
import zipfile
from dataclasses import replace
from unittest.mock import Mock

import pytest
from bs4 import BeautifulSoup

from harmonist import (
    activity_store,
    archive,
    contributions,
    mb_lookup,
    provenance,
    scanner,
    sidecar,
)
from harmonist.bandcamp_hook import write_sidecar_for_item
from harmonist.models import Sidecar
from harmonist.provenance import Origin
from test.helpers import AMAZON_TAGS
from test.test_provenance import assessed
from test.test_scanner import _write_disc
from test.test_upc_contributions import download
from test.test_web import (  # noqa: F401
    _id_for,
    _make_album,
    _make_tagged_album,
    cfg,
    client,
    quiet_sync,
)


def choose(folder, value):
    """Read a persisted choice, including from a previous application run."""
    path = sidecar.sidecar_path(folder)
    data = json.loads(path.read_text())
    data["origin_override"] = value
    path.write_text(json.dumps(data))


def test_cd_choice_overrules_inherited_amazon_comments(tmp_path):
    folder = download(tmp_path, marks=AMAZON_TAGS)
    assert contributions.possible_mismatch(assessed(folder))
    before = {p.name: p.read_bytes() for p in folder.glob("*.m4a")}
    choose(folder, "CD")
    album = scanner.scan(tmp_path)[0]
    assert provenance.origin(album) is Origin.CD
    assert provenance.info(album).why == "You selected this origin."
    assessment = assessed(folder)
    assert assessment.rip
    assert assessment.media_mismatch is False
    assert not contributions.possible_mismatch(assessment)
    # Rematching is another sidecar rewrite, not a change to where audio came from.
    sc = sidecar.read(folder)
    assert sc is not None
    sidecar.write(folder, replace(sc, mb_release_id="another-release"))
    assert provenance.origin(scanner.scan(tmp_path)[0]) is Origin.CD
    assert {p.name: p.read_bytes() for p in folder.glob("*.m4a")} == before


@pytest.mark.parametrize("choice", list(Origin))
def test_every_choice_overrides_a_recorded_download_and_round_trips(tmp_path, choice):
    folder = download(tmp_path, marks={})
    sc = sidecar.read(folder)
    assert sc is not None
    sidecar.write(folder, replace(sc, bandcamp_downloaded=True))
    choose(folder, choice.value)
    sc = sidecar.read(folder)
    assert sc is not None
    sidecar.write(folder, sc)
    assert json.loads(sidecar.sidecar_path(folder).read_text())["origin_override"] == choice
    album = scanner.scan(tmp_path)[0]
    assert provenance.origin(album) is choice
    assert provenance.info(album).why == "You selected this origin."
    assert contributions.downloaded(album) == (choice in provenance.STORES)


def test_explicit_unknown_and_automatic_are_different(tmp_path):
    folder = download(tmp_path, marks=AMAZON_TAGS)
    choose(folder, "Unknown")
    assert provenance.origin(scanner.scan(tmp_path)[0]) is Origin.UNKNOWN
    assert not assessed(folder).eligible
    choose(folder, None)
    sc = sidecar.read(folder)
    assert sc is not None
    sidecar.write(folder, sc)
    assert "origin_override" not in json.loads(sidecar.sidecar_path(folder).read_text())
    assert provenance.origin(scanner.scan(tmp_path)[0]) is Origin.AMAZON


def test_a_different_store_choice_does_not_contribute_an_inherited_bandcamp_url(tmp_path):
    url = "https://artist.bandcamp.com/album/old-download"
    folder = download(tmp_path, marks={"comment": (f"Visit {url}",)})
    sc = sidecar.read(folder)
    assert sc is not None
    sidecar.write(folder, replace(sc, store_url=url, bandcamp_downloaded=True))
    choose(folder, "Qobuz")
    assessment = assessed(folder, formats=("Digital Media",))
    assert assessment.eligible
    assert assessment.store_url is None


@pytest.mark.parametrize("invalid", ["Vinyl", 1, [], {}])
def test_invalid_persisted_origin_is_reported(tmp_path, invalid):
    folder = download(tmp_path)
    choose(folder, invalid)
    with pytest.raises(sidecar.InvalidSidecarError, match="origin_override"):
        sidecar.read(folder)


def test_edit_save_reset_and_noop_preserve_audio_and_record_decisions(client, cfg, monkeypatch):  # noqa: F811
    folder = _make_album(cfg, "Syro", comment="Amazon.com Song ID: 250593508")
    aid = _id_for(cfg, folder)
    audio = next(folder.glob("*.m4a"))
    before = audio.read_bytes()
    fetch = Mock(side_effect=AssertionError("Changing Origin must not fetch MusicBrainz"))
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    editor = BeautifulSoup(client.get(f"/library/{aid}/origin").text, "html.parser")
    selected = editor.select_one("option[selected]")
    assert selected is not None
    assert selected["value"] == "automatic"
    endpoint = f"/library/{aid}/origin"
    response = client.post(endpoint, data={"origin": "CD"})
    assert response.status_code == 200
    assert sidecar.read(folder).origin_override is Origin.CD
    assert audio.read_bytes() == before
    events = activity_store.album_history(aid)
    audit = next(e for e in events if "sidecar.create" in e.message)
    outcome = next(e for e in events if "Origin changed" in e.message)
    assert "origin_override=CD" in audit.message
    assert audit.action_id == outcome.action_id and audit.action_id is not None
    written = sidecar.sidecar_path(folder).stat().st_mtime_ns
    assert client.post(endpoint, data={"origin": "CD"}).status_code == 200
    assert sidecar.sidecar_path(folder).stat().st_mtime_ns == written
    assert activity_store.album_history(aid) == events
    assert client.post(endpoint, data={"origin": "Unknown"}).status_code == 200
    assert provenance.origin(scanner.scan(cfg.paths.music_dir)[0]) is Origin.UNKNOWN
    assert client.post(endpoint, data={"origin": "automatic"}).status_code == 200
    assert sidecar.read(folder).origin_override is None
    assert provenance.origin(scanner.scan(cfg.paths.music_dir)[0]) is Origin.AMAZON
    assert audio.read_bytes() == before
    assert fetch.call_count == 0
    assert any(
        "origin_override=Unknown->None" in e.message for e in activity_store.album_history(aid)
    )


def test_bad_choice_leaves_the_album_unchanged(client, cfg):  # noqa: F811
    folder = _make_album(cfg, "Invalid")
    aid = _id_for(cfg, folder)
    response = client.post(f"/library/{aid}/origin", data={"origin": "Vinyl"})
    assert response.status_code == 400
    assert sidecar.read(folder) is None


def test_failed_origin_write_keeps_an_audited_intent(client, cfg, monkeypatch):  # noqa: F811
    folder = _make_album(cfg, "Unwritable")
    aid = _id_for(cfg, folder)
    monkeypatch.setattr(sidecar, "write", Mock(side_effect=OSError("read-only filesystem")))
    with pytest.raises(OSError, match="read-only filesystem"):
        client.post(f"/library/{aid}/origin", data={"origin": "CD"})
    events = activity_store.album_history(aid)
    intent = next(e for e in events if "origin.set" in e.message)
    assert "origin=CD" in intent.message
    assert intent.action_id is not None
    assert not any("Origin changed" in e.message for e in events)
    assert sidecar.read(folder) is None


def test_multi_folder_choices_are_resolved_for_every_part(client, cfg):  # noqa: F811
    mbid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    folders = [cfg.paths.music_dir / f"Disc {i}" for i in (1, 2)]
    for i, folder in enumerate(folders, 1):
        folder.mkdir()
        _write_disc(folder, disc=i, n=1, album="Two discs", mbid=mbid)
        sidecar.write(
            folder,
            Sidecar(mb_release_id=mbid, notes=f"part {i}", accepted_release_id=mbid),
        )
    choose(folders[0], "CD")
    choose(folders[1], "Amazon")
    albums = scanner.scan(cfg.paths.music_dir)
    assert len(albums) == 1
    assert provenance.origin(albums[0]) is Origin.UNKNOWN
    assert "differ" in provenance.info(albums[0]).why
    endpoint = f"/library/{albums[0].id}/origin"
    assert client.post(endpoint, data={"origin": "CD"}).status_code == 200
    for i, folder in enumerate(folders, 1):
        sc = sidecar.read(folder)
        assert sc.origin_override is Origin.CD
        assert sc.notes == f"part {i}"
        assert sc.accepted_release_id is None
    assert provenance.origin(scanner.scan(cfg.paths.music_dir)[0]) is Origin.CD
    assert not scanner.scan(cfg.paths.music_dir)[0].origin_override_conflict
    assert client.post(endpoint, data={"origin": "automatic"}).status_code == 200
    assert all(sidecar.read(folder).origin_override is None for folder in folders)


@pytest.mark.parametrize("choice", ["CD", "automatic"])
def test_changing_origin_reopens_a_dismissed_mismatch(client, cfg, choice):  # noqa: F811
    folder = _make_album(cfg, "Accepted", mbid="rel-accepted")
    sidecar.write(
        folder,
        Sidecar(
            mb_release_id="rel-accepted",
            origin_override=Origin.AMAZON,
            accepted_release_id="rel-accepted",
        ),
    )
    aid = _id_for(cfg, folder)
    endpoint = f"/library/{aid}/origin"
    before = activity_store.album_history(aid)
    assert client.post(endpoint, data={"origin": "Amazon"}).status_code == 200
    assert sidecar.read(folder).accepted_release_id == "rel-accepted"
    assert activity_store.album_history(aid) == before
    assert client.post(endpoint, data={"origin": choice}).status_code == 200
    assert sidecar.read(folder).accepted_release_id is None
    assert any(
        "accepted_release_id=rel-accepted->None" in e.message
        for e in activity_store.album_history(aid)
    )


def test_redownload_keeps_the_old_choice_in_the_archive_not_the_new_files(client, cfg, quiet_sync):  # noqa: F811
    folder = _make_tagged_album(cfg, "Replacement", mbid="rel-origin", item_id=8001, tagged_at=None)
    choose(folder, "CD")
    response = client.post(f"/library/{_id_for(cfg, folder)}/redownload")
    assert response.status_code == 200
    archives = list(cfg.paths.music_dir.rglob("*.zip"))
    assert len(archives) == 1
    with zipfile.ZipFile(archives[0]) as saved:
        name = next(n for n in saved.namelist() if n.endswith(".harmonist.json"))
        assert json.loads(saved.read(name))["origin_override"] == "CD"
    replacement = _make_album(cfg, "Replacement")
    item = Mock(item_id=8001, _data={"item_url": "https://x.bandcamp.com/album/replacement"})
    assert write_sidecar_for_item(item, replacement, downloaded=True)
    assert sidecar.read(replacement).origin_override is None
    assert provenance.origin(scanner.scan(cfg.paths.music_dir)[0]) is Origin.BANDCAMP


def test_failed_redownload_preserves_the_choice(client, cfg, quiet_sync, monkeypatch):  # noqa: F811
    folder = _make_tagged_album(
        cfg, "Failed", mbid="rel-origin-failed", item_id=8002, tagged_at=None
    )
    choose(folder, "CD")
    monkeypatch.setattr(
        archive, "archive_and_remove", Mock(side_effect=archive.ArchiveError("failed"))
    )
    response = client.post(f"/library/{_id_for(cfg, folder)}/redownload")
    assert response.status_code == 500
    assert sidecar.read(folder).origin_override is Origin.CD
    assert list(folder.glob("*.m4a"))
