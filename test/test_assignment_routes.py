"""Real-file coverage of Needs MBID assignment review and confirmation."""

import re

import pytest

from harmonist import album_files, formats
from test import test_web
from test.test_web import _confirmation_fields, _confirmation_setup, _id_for

cfg = test_web.cfg
client = test_web.client


def test_read_only_comparison_uses_the_editor_rows_without_network(client, cfg, monkeypatch):
    from bs4 import BeautifulSoup

    from harmonist import mb_cache

    d, *_ = _confirmation_setup(cfg, monkeypatch, old_mbid=None)
    aid = _id_for(cfg, d)
    # Seed the normal cache once; rendering either mode must not refresh it.
    editor = client.get(f"/assignments/{aid}")
    monkeypatch.setattr(mb_cache, "fetch_release", lambda *a, **k: pytest.fail("unexpected fetch"))
    view = client.get(f"/assignments/{aid}?cancel=true")

    def rows(html):
        return [
            row.get_text(" ", strip=True).replace("↑", "").replace("↓", "").split()
            for row in BeautifulSoup(html, "html.parser").select("[data-assignment-row]")
        ]

    assert rows(view.text) == rows(editor.text)
    assert len(rows(view.text)) == 2
    assert not BeautifulSoup(view.text, "html.parser").select('button[name="move"]')


def test_release_only_preview_from_suggestion_requires_confirmation(client, cfg, monkeypatch):
    d, _release, _big, _small, calls = _confirmation_setup(cfg, monkeypatch, old_mbid=None)
    files = album_files.audio_files(d)
    before = [f.read_bytes() for f in files]
    aid = _id_for(cfg, d)
    preview = client.get(f"/confirm/{aid}/preview?release_only=true")
    fields = _confirmation_fields(preview.text)
    assert fields["disk_order"] == "0,1,-,-"
    assert fields["mb_order"] == "-,-,0,1"
    assert "Tracks unassigned" in preview.text
    assert [f.read_bytes() for f in files] == before
    assert [call for call in calls if call[0] == "mb"] == [("mb", "rel-new-confirm")]


def test_uncached_comparison_waits_for_explicit_refresh(client, cfg, monkeypatch):
    from bs4 import BeautifulSoup

    d, _release, _big, _small, calls = _confirmation_setup(cfg, monkeypatch, old_mbid=None)
    aid = _id_for(cfg, d)
    page = client.get("/tasks")
    loader = BeautifulSoup(page.text, "html.parser").select_one(
        f'[hx-get="/assignments/{aid}?cancel=true&on_album_page=false"]'
    )
    assert loader is not None
    view = client.get(loader["hx-get"])
    assert "Track comparison is not cached" in view.text
    assert calls == []
    refresh = BeautifulSoup(view.text, "html.parser").select_one('[hx-get*="reread=true"]')
    assert refresh is not None
    loaded = client.get(refresh["hx-get"])
    assert "2 files · 2 MusicBrainz tracks" in loaded.text
    assert not BeautifulSoup(loaded.text, "html.parser").select('button[name="move"]')
    assert calls == [("mb", "rel-new-confirm")]
    # Explicit refresh must fetch even once the cache has been populated.
    loaded = client.get(refresh["hx-get"])
    assert "2 files · 2 MusicBrainz tracks" in loaded.text
    assert calls == [("mb", "rel-new-confirm"), ("mb", "rel-new-confirm")]


@pytest.mark.parametrize("tracks", [1, 2, 3])
def test_read_only_confirmation_uses_current_counts_and_displayed_mapping(
    client, cfg, monkeypatch, tracks
):
    from copy import deepcopy

    from bs4 import BeautifulSoup

    d, release, *_ = _confirmation_setup(cfg, monkeypatch, old_mbid=None)
    listing = release["medium-list"][0]["track-list"]
    if tracks == 1:
        listing.pop()
    elif tracks == 3:
        extra = deepcopy(listing[-1])
        extra.update(id="extra-track", position="3", number="3")
        listing.append(extra)
    aid = _id_for(cfg, d)
    view = client.get(f"/assignments/{aid}?cancel=true&reread=true")
    assert f"2 files · {tracks} MusicBrainz tracks" in view.text
    soup = BeautifulSoup(view.text, "html.parser")
    confirm = soup.select_one(f'button[hx-post="/confirm/{aid}/preview"]')
    assert confirm is not None and confirm.get_text(strip=True) == "Confirm release"
    preview = client.post(f"/confirm/{aid}/preview", data=_confirmation_fields(view.text))
    assert (
        _confirmation_fields(preview.text)["disk_order"]
        == _confirmation_fields(view.text)["disk_order"]
    )
    if tracks == 1:
        assert "Unassigned files will receive the album's MusicBrainz ID" in preview.text
        applied = client.post(f"/confirm/{aid}", data=_confirmation_fields(preview.text))
        assert "confirmation-applied" in applied.headers.get("HX-Trigger", "")


def test_assignment_review_writes_the_pairing_the_user_moved(client, cfg, monkeypatch):
    from harmonist import activity_store, sidecar

    d, release, *_ = _confirmation_setup(cfg, monkeypatch, old_mbid=None)
    aid = _id_for(cfg, d)
    files = album_files.audio_files(d)
    before = [f.read_bytes() for f in files]
    before_tags = [formats.read_owned(f) for f in files]
    editor = client.get(f"/assignments/{aid}")
    assert editor.status_code == 200
    moved = client.post(
        f"/assignments/{aid}",
        data=_confirmation_fields(editor.text) | {"move": "disk:0:down"},
    )
    assert moved.status_code == 200
    fields = _confirmation_fields(moved.text)
    assert fields["disk_order"] == "1,0"
    preview = client.post(f"/confirm/{aid}/preview", data=fields)
    assert preview.status_code == 200
    assert [f.read_bytes() for f in files] == before
    result = client.post(f"/confirm/{aid}", data=_confirmation_fields(preview.text))
    assert "confirmation-applied" in result.headers.get("HX-Trigger", "")
    expected = release["medium-list"][0]["track-list"]
    assert formats.read_tags(files[0]).title == expected[1]["title"]
    assert formats.read_tags(files[0]).track_num == 2
    assert formats.read_tags(files[1]).track_num == 1
    new_id = _id_for(cfg, d)
    assert new_id != aid
    events = activity_store.album_history(new_id)
    anchor = next(e.id for e in events if e.message == "Tagged")
    assert any(e.message.startswith("tag.track") for e in events)
    undo = client.post(f"/tags/restore/{new_id}", data={"event_id": anchor})
    assert undo.status_code == 200
    assert [formats.read_owned(f) for f in files] == before_tags
    assert sidecar.read(d).mb_release_id is None


@pytest.mark.parametrize("change", ["file", "rename", "release"])
def test_assignment_confirmation_refuses_changed_inputs(client, cfg, monkeypatch, change):
    from mutagen.mp4 import MP4

    d, release, *_ = _confirmation_setup(cfg, monkeypatch)
    aid = _id_for(cfg, d)
    editor = client.get(f"/assignments/{aid}")
    assert editor.status_code == 200
    preview = client.post(f"/confirm/{aid}/preview", data=_confirmation_fields(editor.text))
    files = album_files.audio_files(d)
    if change == "file":
        audio = MP4(files[0])
        audio["\xa9nam"] = ["Edited externally"]
        audio.save()
    elif change == "rename":
        files[0] = files[0].rename(d / "Renamed.m4a")
    else:
        release["medium-list"][0]["track-list"][0]["title"] = "Changed upstream"
    before = [f.read_bytes() for f in files]
    result = client.post(f"/confirm/{aid}", data=_confirmation_fields(preview.text))
    assert "changed" in result.text.lower()
    assert "confirmation-applied" not in result.headers.get("HX-Trigger", "")
    assert [f.read_bytes() for f in files] == before


def test_untagged_editor_names_the_file_and_missing_numbers(client, cfg, monkeypatch):
    from bs4 import BeautifulSoup
    from mutagen.mp4 import MP4

    d, *_ = _confirmation_setup(cfg, monkeypatch, old_mbid=None)
    for f in album_files.audio_files(d):
        audio = MP4(f)
        for key in ("\xa9nam", "trkn", "disk"):
            audio.pop(key, None)
        audio.save()
    editor = client.get(f"/assignments/{_id_for(cfg, d)}")
    assert editor.status_code == 200
    assert "On disk" in editor.text and "MusicBrainz" in editor.text
    for f in album_files.audio_files(d):
        assert f.name in editor.text
    rows = BeautifulSoup(editor.text, "html.parser").select("[data-assignment-row]")
    for row, path in zip(rows, album_files.audio_files(d), strict=True):
        assert next(row.find_all("td")[0].stripped_strings) == "?"
        assert next(row.find_all("td")[1].stripped_strings) == path.name
    assert re.search(r'name="disk_order" value="0,1"', editor.text)


def test_extra_file_can_move_past_a_gap_but_cannot_be_silently_dropped(client, cfg, monkeypatch):
    d, release, *_ = _confirmation_setup(cfg, monkeypatch)
    release["medium-list"][0]["track-list"].pop()
    aid = _id_for(cfg, d)
    files = album_files.audio_files(d)
    before = [f.read_bytes() for f in files]
    editor = client.get(f"/assignments/{aid}")
    fields = _confirmation_fields(editor.text)
    assert fields["mb_order"] == "0,-"
    moved = client.post(f"/assignments/{aid}", data=fields | {"move": "mb:1:up"})
    fields = _confirmation_fields(moved.text)
    assert fields["mb_order"] == "-,0"
    assert "Accept changes" in moved.text
    preview = client.post(f"/confirm/{aid}/preview", data=fields)
    assert "unassigned file already has MusicBrainz track IDs" in preview.text
    attempted = client.post(f"/confirm/{aid}", data=_confirmation_fields(preview.text))
    assert "confirmation-applied" not in attempted.headers.get("HX-Trigger", "")
    assert [f.read_bytes() for f in files] == before


def test_arrow_requests_use_the_cache_and_never_fetch_artwork(client, cfg, monkeypatch):
    d, _release, _big, _small, calls = _confirmation_setup(cfg, monkeypatch)
    aid = _id_for(cfg, d)
    editor = client.get(f"/assignments/{aid}")
    for move in ("disk:0:down", "mb:0:down", "disk:1:up"):
        editor = client.post(
            f"/assignments/{aid}", data=_confirmation_fields(editor.text) | {"move": move}
        )
        assert editor.status_code == 200
    assert calls == [("mb", "rel-new-confirm")]


@pytest.mark.parametrize("disc", [1, 2])
def test_editor_keeps_disc_context_and_number_changes_with_entries(client, cfg, monkeypatch, disc):
    from bs4 import BeautifulSoup
    from mutagen.mp4 import MP4

    d, release, *_ = _confirmation_setup(cfg, monkeypatch, old_mbid=None)
    release["medium-list"][0]["position"] = str(disc)
    for i, path in enumerate(album_files.audio_files(d), start=1):
        audio = MP4(path)
        audio["disk"] = [(disc, disc)]
        audio["trkn"] = [(i, 2)]
        audio.save()
    aid = _id_for(cfg, d)
    editor = client.get(f"/assignments/{aid}")
    moved = client.post(
        f"/assignments/{aid}",
        data=_confirmation_fields(editor.text) | {"move": "disk:0:down"},
    )
    rows = BeautifulSoup(moved.text, "html.parser").select("[data-assignment-row]")
    cells = rows[0].find_all("td", recursive=False)
    assert cells[0].select_one("span").text.strip() == "2"
    assert cells[3].select_one("span").text.strip() == "1"
    assert f"Track numbering changes from {disc}.2 to {disc}.1" in str(cells[3])
    headings = [div.text.strip() for div in cells[0].find_all("div")]
    assert headings == (["Disc 2"] if disc == 2 else [])
    assert "bg-amber-50" in rows[0]["class"]


def test_no_match_candidate_has_an_actionable_description(client, cfg, monkeypatch):
    from dataclasses import replace

    from harmonist import sidecar

    d, *_ = _confirmation_setup(cfg, monkeypatch)
    sc = sidecar.read(d)
    assert sc is not None and sc.mb_match_candidate is not None
    sidecar.write(
        d, replace(sc, mb_match_candidate=replace(sc.mb_match_candidate, confidence="no_match"))
    )
    aid = _id_for(cfg, d)
    client.get(f"/assignments/{aid}")
    body = client.get(f"/assignments/{aid}?cancel=true").text
    assert "Edit track assignments</button>" in body
    assert "2 files · 2 MusicBrainz tracks" in body


def test_preview_retry_preserves_the_assignment_after_a_musicbrainz_failure(
    client, cfg, monkeypatch
):
    from harmonist import mb_lookup

    d, *_ = _confirmation_setup(cfg, monkeypatch)
    aid = _id_for(cfg, d)
    editor = client.get(f"/assignments/{aid}")
    fields = _confirmation_fields(editor.text) | {"disk_order": "1,0"}

    def unavailable(*args, **kwargs):
        raise mb_lookup.MBError("busy")

    with monkeypatch.context() as patch:
        patch.setattr("harmonist.mb_cache.fetch_release", unavailable)
        failed = client.post(f"/confirm/{aid}/preview", data=fields)
    retry = _confirmation_fields(failed.text)
    assert retry["disk_order"] == "1,0"
    assert retry["release_fingerprint"] == fields["release_fingerprint"]
    restored = client.post(f"/confirm/{aid}/preview", data=retry)
    assert _confirmation_fields(restored.text)["disk_order"] == "1,0"
