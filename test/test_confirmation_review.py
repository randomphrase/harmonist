"""Confirmation continues a reviewed snapshot without network requests."""

from datetime import timedelta

import pytest

from harmonist import mb_cache
from test import test_web
from test.test_web import _confirmation_fields, _confirmation_setup, _id_for, _review_with_artwork

cfg = test_web.cfg
client = test_web.client


@pytest.mark.parametrize("action", ["apply", "accept"])
@pytest.mark.parametrize("included", [False, True])
def test_reviewed_confirmation_does_not_refetch_expired_data(
    client, cfg, monkeypatch, action, included
):
    root, _release, *_rest, calls = _confirmation_setup(cfg, monkeypatch, old_mbid=None)
    aid = _id_for(cfg, root)
    _, _, fields = _review_with_artwork(client, aid)
    fields["include_artwork"] = str(included).lower()
    calls.clear()
    monkeypatch.setattr(mb_cache, "_ttl", timedelta(0))
    endpoint = f"/confirm/{aid}" + ("" if action == "apply" else f"/{action}")
    response = client.post(endpoint, data=fields)
    assert response.status_code == 200
    assert calls == []
    assert "confirmation-applied" in response.headers.get("HX-Trigger", "")


@pytest.mark.parametrize("missing", [False, True])
def test_review_uses_its_snapshot_or_stops_without_fetching(client, cfg, monkeypatch, missing):
    from harmonist import album_files, formats

    root, release, *_rest, calls = _confirmation_setup(cfg, monkeypatch, old_mbid=None)
    aid = _id_for(cfg, root)
    fields = _confirmation_fields(client.get(f"/assignments/{aid}").text)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    reviewed_title = release["title"]
    release["title"] = "Not reviewed upstream change"
    if missing:
        monkeypatch.setattr(mb_cache, "stored_release", lambda mbid: None)
    calls.clear()
    response = client.post(f"/confirm/{aid}", data=fields)
    assert calls == []
    if missing:
        assert "reviewed release is no longer available" in response.text
        assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    else:
        assert "confirmation-applied" in response.headers.get("HX-Trigger", "")
        assert all(
            formats.read_owned(path)["album"] == reviewed_title
            for path in album_files.audio_files(root)
        )


def test_partial_review_explains_unassigned_files_and_applies_once(client, cfg, monkeypatch):
    from bs4 import BeautifulSoup

    root, release, *_rest, calls = _confirmation_setup(cfg, monkeypatch, old_mbid=None)
    release["medium-list"][0]["track-list"].pop()
    aid = _id_for(cfg, root)
    editor = client.get(f"/assignments/{aid}")
    rows = BeautifulSoup(editor.text, "html.parser").select("[data-assignment-row]")
    assert "Unassigned" in rows[-1].text
    assert "Unassigned files will receive the album's MusicBrainz ID" in editor.text
    calls.clear()
    confirmation = client.post(f"/confirm/{aid}/accept", data=_confirmation_fields(editor.text))
    assert "confirmation-applied" in confirmation.headers.get("HX-Trigger", "")
    assert calls == []


def test_reviewed_store_url_recovery_does_not_query_musicbrainz(client, cfg, monkeypatch):
    from dataclasses import replace
    from unittest.mock import Mock

    from mutagen.mp4 import MP4

    from harmonist import album_files, sidecar

    root, *_ = _confirmation_setup(cfg, monkeypatch, old_mbid=None)
    original = sidecar.read(root)
    assert original is not None
    sidecar.write(root, replace(original, store_url=None))
    for path in album_files.audio_files(root):
        audio = MP4(path)
        audio["\xa9cmt"] = ["https://example.bandcamp.com"]
        audio.save()
    aid = _id_for(cfg, root)
    fields = _confirmation_fields(client.get(f"/assignments/{aid}").text)
    fetch_urls = Mock(return_value=["https://example.bandcamp.com/album/example"])
    monkeypatch.setattr("harmonist.mb_lookup.fetch_release_urls", fetch_urls)
    response = client.post(f"/confirm/{aid}", data=fields)
    assert "confirmation-applied" in response.headers.get("HX-Trigger", "")
    fetch_urls.assert_not_called()
    assert sidecar.read(root).store_url == "https://example.bandcamp.com"
