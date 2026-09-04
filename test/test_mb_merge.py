"""What happens when MusicBrainz has merged the release an album names (#268).

MusicBrainz *redirects* a merged MBID: `fetch_release(old)` answers with the
**target** release, carrying a different `id`. Two writers then disagree —
`tagger._build_tagset` puts `release["id"]` (the new id) into every file while
`_tag_with_release` wrote the *requested* id to the sidecar. The album derives
TAGGING off that disagreement, the Inbox kicks the reconciler, and reconcile
adopts the file tags: an identity change laundered through machinery built for
"the user re-tagged in Picard", with nothing anywhere saying a merge happened.

Merges are common, and #32's nightly pass would walk into every one that has
happened since an album was tagged — so the album has to come out of a re-tag
settled, and its History has to say why its identity moved.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mutagen.mp4 import MP4

from harmonist import activity_store, mb_lookup, scanner
from harmonist import sidecar as sidecar_mod
from harmonist.config import BandcampConfig, Config, PathsConfig, ServerConfig, TestConfig
from harmonist.models import AlbumState, Sidecar
from harmonist.web.main import create_app

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SINE_M4A = FIXTURES_DIR / "sine.m4a"

OLD_MBID = "11111111-2222-3333-4444-555555555555"
NEW_MBID = "99999999-8888-7777-6666-555555555555"
TAGGED_AT = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def cfg(tmp_path):
    return Config(
        paths=PathsConfig(config_dir=tmp_path / "config", music_dir=tmp_path / "music"),
        bandcamp=BandcampConfig(),
        server=ServerConfig(),
        test=TestConfig(mode="fixture"),
    )


@pytest.fixture(autouse=True)
def no_cover_fetch(monkeypatch):
    """No cover-art requests: this module is about identity, and CAA is off-box."""
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)


@pytest.fixture
def client(cfg):
    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.config_dir.mkdir(parents=True, exist_ok=True)
    # HX-Request: the CSRF middleware requires it on every state-changing call.
    return TestClient(create_app(cfg), headers={"HX-Request": "true"})


def _release(mbid: str) -> dict:
    """A 2-track CD carrying `mbid` as its own id."""
    return {
        "id": mbid,
        "title": "Test Album",
        "status": "Official",
        "artist-credit": [{"artist": {"id": "art-1", "name": "Artist"}, "name": "Artist"}],
        "release-group": {"id": "rg-1", "primary-type": "Album"},
        "medium-list": [
            {
                "position": "1",
                "format": "CD",
                "track-list": [
                    {
                        "id": f"rt-{i}",
                        "position": str(i),
                        "title": f"Track {i}",
                        "recording": {"id": f"rec-{i}", "title": f"Track {i}", "length": "1000"},
                    }
                    for i in range(1, 3)
                ],
            }
        ],
    }


def _redirects_to(monkeypatch, mbid: str) -> None:
    """Every fetch answers with the release `mbid` names, whatever was asked for
    — which is exactly what MusicBrainz does for an id that has been merged
    away, and the only signal there is that it happened."""
    monkeypatch.setattr(mb_lookup, "fetch_release", lambda requested: _release(mbid))


def _album(cfg) -> Path:
    """A 2-track album tagged as OLD_MBID, sitting COMPLETE in the Library."""
    d = cfg.paths.music_dir / "Artist" / "Album"
    d.mkdir(parents=True)
    for i in (1, 2):
        f = d / f"0{i} Track {i}.m4a"
        shutil.copy(SINE_M4A, f)
        a = MP4(f)
        a["\xa9alb"] = ["Test Album"]
        a["\xa9ART"] = ["Artist"]
        a["trkn"] = [(i, 2)]
        a["----:com.apple.iTunes:MusicBrainz Album Id"] = [OLD_MBID.encode()]
        a.save()
    sidecar_mod.write(d, Sidecar(mb_release_id=OLD_MBID, tagged_at=TAGGED_AT))
    return d


def _scanned(cfg, album_dir: Path):
    for a in scanner.scan(cfg.paths.music_dir):
        if a.path == album_dir:
            return a
    raise AssertionError(f"no album at {album_dir}")


def _file_mbids(album_dir: Path) -> set[str]:
    key = "----:com.apple.iTunes:MusicBrainz Album Id"
    return {MP4(f)[key][0].decode() for f in sorted(album_dir.glob("*.m4a"))}


def _messages() -> list[str]:
    return [e.message for e in activity_store.recent(50)]


def test_a_merge_leaves_the_sidecar_and_the_files_naming_the_same_release(client, cfg, monkeypatch):
    """The two writers have to agree. The files get `release["id"]` and always
    did; the sidecar used to get the id that was *asked for*, which after a
    redirect names a release that no longer exists."""
    d = _album(cfg)
    _redirects_to(monkeypatch, NEW_MBID)

    r = client.post(f"/retag/{_scanned(cfg, d).id}")
    assert r.status_code == 200

    after = sidecar_mod.read(d)
    assert after is not None
    assert after.mb_release_id == NEW_MBID
    assert _file_mbids(d) == {NEW_MBID}


def test_a_merge_does_not_leave_the_album_looking_externally_retagged(client, cfg, monkeypatch):
    """The disagreement derived TAGGING (`scanner._derive_state`), so the Inbox
    picked the album up and reconcile rewrote its identity from the file tags —
    self-healing, but through a path meant for a re-tag done in Picard."""
    d = _album(cfg)
    _redirects_to(monkeypatch, NEW_MBID)

    client.post(f"/retag/{_scanned(cfg, d).id}")

    assert _scanned(cfg, d).state == AlbumState.COMPLETE


def test_a_merge_says_so_in_the_albums_history_naming_both_releases(client, cfg, monkeypatch):
    """A merge is qualitatively different from a tag change, and usually arrives
    with one. Nothing said it had happened.

    Asserted against `album_history`, which is the surface the album's History
    page renders: it unions activity and audit over the alias chain, so this
    covers the entry being *attributed* to the album as well as written.

    The two conditions are deliberately on ONE message. Split, they both pass
    for free: `sidecar.update` already renders `mbid=old->new` whenever an
    album's identity moves, so "some line names both ids" is true of a user
    re-matching an album by hand — which is the very thing a merge has been
    indistinguishable from. Naming it is the whole point of the record.
    """
    d = _album(cfg)
    _redirects_to(monkeypatch, NEW_MBID)

    client.post(f"/retag/{_scanned(cfg, d).id}")

    history = [e.message for e in activity_store.album_history(NEW_MBID)]
    assert [m for m in history if "merged" in m.lower() and OLD_MBID in m and NEW_MBID in m], (
        history
    )
    # And in the user's own language, in the feed — the ids above are forensics.
    feed = [e.message for e in activity_store.recent(50, source=activity_store.Source.ACTIVITY)]
    assert [m for m in feed if "merged" in m.lower()], feed


def test_a_merge_links_the_old_id_to_the_new_one(client, cfg, monkeypatch):
    """Everything recorded under the old id — the album's whole history, and any
    deep link already written into the feed — is orphaned without the alias."""
    d = _album(cfg)
    _redirects_to(monkeypatch, NEW_MBID)

    client.post(f"/retag/{_scanned(cfg, d).id}")

    assert activity_store.resolve_alias(OLD_MBID) == NEW_MBID


def test_a_second_retag_after_a_merge_reports_nothing(client, cfg, monkeypatch):
    """Idempotency, which is the invariant #32 is built on: the second pass over
    a merged album finds the id it asked for is the id it got, so there is
    nothing to name. Without it a nightly pass would re-announce the same merge
    every night for the rest of the album's life."""
    d = _album(cfg)
    _redirects_to(monkeypatch, NEW_MBID)

    client.post(f"/retag/{_scanned(cfg, d).id}")
    before = len([m for m in _messages() if "merged" in m.lower()])
    client.post(f"/retag/{_scanned(cfg, d).id}")

    assert before == 2, _messages()  # the activity entry and its audit line
    assert len([m for m in _messages() if "merged" in m.lower()]) == before, _messages()
    assert _scanned(cfg, d).state == AlbumState.COMPLETE


def test_an_ordinary_retag_says_nothing_about_a_merge(client, cfg, monkeypatch):
    """The control, and the invariant #32 cares about: don't narrate a no-op.
    A re-tag against a release that has not moved must not mention one."""
    d = _album(cfg)
    _redirects_to(monkeypatch, OLD_MBID)

    client.post(f"/retag/{_scanned(cfg, d).id}")

    assert not [m for m in _messages() if "merged" in m.lower()], _messages()
    assert activity_store.resolve_alias(OLD_MBID) is None


# ---------- what the album's page says while the re-tag is still outstanding (#361) ----------
#
# Everything above drives the merge through POST /retag, and that is where #268's
# guarantee is written. The window BEFORE that re-tag was covered by nothing: the
# fetch redirects, `plan_album` reports `mb_album_id: old -> new`, the album is
# flagged Update available on the strength of it, and the only thing on the page
# that said why was a row in the re-tag box — filed under a heading calling it one
# of the "other tags a re-tag would change".
#
# The web-route rung is the one that can see this: the note is discovered by the
# /compare fetch and rendered into the response, so the rung below (a template
# render) would only assert the markup it was handed, and the rung above (a
# browser) buys nothing — the button is an ordinary hx-post, and the panel gains
# no interaction the page did not already have.


def _compare(client, cfg, album_dir: Path, *, reread: bool = False) -> str:
    """The /compare response for `album_dir` — where a merge is found out about.

    `reread` is the page's "read again" control (#127), and a test needs it
    wherever the album has been tagged first: that tagging stored a release row
    under the id it asked for, and a merge is only ever visible on a LIVE fetch,
    since a redirect is something MusicBrainz does rather than something the
    cached payload records.
    """
    r = client.get(f"/library/{_scanned(cfg, album_dir).id}/compare{'?reread=1' if reread else ''}")
    assert r.status_code == 200, r.text
    return r.text


def test_a_merge_is_stated_on_the_album_panel_naming_the_surviving_release(
    client, cfg, monkeypatch
):
    """The album's identity has moved, and the panel's MusicBrainz badge is the
    thing whose meaning changed — so that is where it is said, in the album's own
    terms and with the release MusicBrainz now serves linked by name."""
    d = _album(cfg)
    _redirects_to(monkeypatch, NEW_MBID)

    html = _compare(client, cfg, d)

    assert "merged this release" in html, html
    assert f"https://musicbrainz.org/release/{NEW_MBID}" in html, html


def test_a_merge_is_not_also_listed_as_a_pending_tag_change(client, cfg, monkeypatch):
    """One statement, in one place. The re-tag box states what a re-tag would
    change in the fields nothing else on the page shows, and the release id is no
    longer one of them — restating it there would put the identity change under a
    heading about leftover tags, directly beneath the note that already says it."""
    d = _album(cfg)
    _redirects_to(monkeypatch, NEW_MBID)

    html = _compare(client, cfg, d)

    # `MusicBrainz release` is `mb_album_id`'s label, and the box — which renders
    # through the same partial a History entry does — is the only thing in this
    # response that can emit it as a change row. Matched with its tag, because
    # the phrase alone also occurs inside the Checked line's tooltip.
    assert "<dt>MusicBrainz release</dt>" not in html, html


def test_an_album_whose_release_has_not_moved_says_nothing_about_a_merge(client, cfg, monkeypatch):
    """The control, and the same one the re-tag path keeps: don't narrate a
    no-op. Every album in the library reaches this code, and all but a handful
    got the release they asked for."""
    d = _album(cfg)
    _redirects_to(monkeypatch, OLD_MBID)

    # The sentence, not the word: the note's wrapper is on the page either way,
    # empty, so that its id is there for the out-of-band swap to find.
    assert "merged this release" not in _compare(client, cfg, d)


def test_a_merge_alone_still_offers_the_re_tag_that_resolves_it(client, cfg, monkeypatch):
    """The dead end this closes (#291): an album flagged Update available whose
    page offers nothing to do about it.

    A merge can be the ONLY thing outstanding — the release payload is otherwise
    the one the files were tagged from — and such an album is `advisory`, so the
    update section beneath the panel is not drawn and takes the Re-tag button
    with it. The note carries its own remedy for exactly the reason the
    partial-tag badge does.
    """
    d = _album(cfg)
    # Tag the files from the release as it stands, so nothing but the id is left
    # to differ once MusicBrainz moves it.
    _redirects_to(monkeypatch, OLD_MBID)
    client.post(f"/retag/{_scanned(cfg, d).id}")
    _redirects_to(monkeypatch, NEW_MBID)

    html = _compare(client, cfg, d, reread=True)

    assert "merged this release" in html, html
    assert html.count(f'hx-post="/retag/{_scanned(cfg, d).id}"') == 1, html


def test_a_merge_arriving_with_a_tag_update_is_answered_by_one_button(client, cfg, monkeypatch):
    """The other half of the same rule, and the commoner case: a merge usually
    arrives WITH a tag update (#268), so the update section beneath the panel is
    drawn and already carries **Re-tag from MB**.

    The note then states the identity change and names the remedy in words,
    without adding a second control that does the same POST in different words —
    the duplication #360 and #366 were both about. Counted rather than located:
    which of the two surfaces holds the button is the decision under test, and a
    page carrying both would pass any assertion naming just one of them.
    """
    d = _album(cfg)  # tagged with four atoms, so most of the release differs
    _redirects_to(monkeypatch, NEW_MBID)

    html = _compare(client, cfg, d)

    assert "merged this release" in html, html
    assert "Re-tag from MB" in html, html
    assert html.count(f'hx-post="/retag/{_scanned(cfg, d).id}"') == 1, html


# ---------- where the album lives once the re-tag has followed the merge (#375) ----------
#
# A merge MOVES the album's id, and the page the re-tag was pressed on is at the
# old one. `album-retagged` used to be a bare `true`, so the only thing the page
# could do with it was reload itself — at the address it was already on, which
# names a release the album no longer claims. The alias chain still resolves it,
# so what came back was right; the address bar, a bookmark taken from it and a
# refresh afterwards all stayed pointed at the superseded release.
#
# The event carries the album's live id now, and the page navigates when it has
# moved. This rung sees the header; whether the browser then goes there is
# `test/e2e/test_merge_retag.py`.


def _retagged(response) -> dict:
    """The `album-retagged` event the re-tag came back with."""
    triggers = json.loads(response.headers["HX-Trigger"])
    assert "album-retagged" in triggers, triggers
    return triggers["album-retagged"]


def test_a_retag_following_a_merge_says_which_album_the_page_should_go_to(client, cfg, monkeypatch):
    """The id the album has AFTER the tagging, which is the surviving release —
    not the one the request was addressed to."""
    d = _album(cfg)
    _redirects_to(monkeypatch, NEW_MBID)

    r = client.post(f"/retag/{_scanned(cfg, d).id}")

    assert _retagged(r) == {"album_id": NEW_MBID}


def test_an_ordinary_retag_says_the_album_is_where_it_was(client, cfg, monkeypatch):
    """The control, and what stops the page navigating on every re-tag: an album
    whose release has not moved comes back naming the id it already had, so the
    page reloads in place. Read off the sidecar rather than assumed, which is why
    this can disagree with the test above."""
    d = _album(cfg)
    _redirects_to(monkeypatch, OLD_MBID)

    r = client.post(f"/retag/{_scanned(cfg, d).id}")

    assert _retagged(r) == {"album_id": OLD_MBID}
