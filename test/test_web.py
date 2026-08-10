"""Smoke tests for the FastAPI layer.

Not exhaustive — task 13 owns the comprehensive integration test matrix.
These verify wiring: routes load, scanner integration works, state-dispatched
templates render without crashing for each AlbumState.
"""

from __future__ import annotations

import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient
from mutagen.mp4 import MP4

from harmonist import mb_lookup
from harmonist import sidecar as sc
from harmonist.activity_store import Level
from harmonist.config import BandcampConfig, Config, PathsConfig, ServerConfig, TestConfig
from harmonist.models import BandcampInfo, MatchCandidate, Sidecar, TrackComparison
from harmonist.tagger import (
    ATOM_ALBUM,
    ATOM_ARTIST,
    ATOM_COMMENT,
    ATOM_MB_ALBUM_ID,
    ATOM_TITLE,
)
from harmonist.web.main import create_app

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SINE_M4A = FIXTURES_DIR / "sine.m4a"


@pytest.fixture
def cfg(tmp_path):
    return Config(
        paths=PathsConfig(
            config_dir=tmp_path / "config",
            music_dir=tmp_path / "music",
        ),
        bandcamp=BandcampConfig(),
        server=ServerConfig(),
        test=TestConfig(mode="fixture"),
    )


@pytest.fixture
def client(cfg):
    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.config_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(cfg)
    # The CSRF middleware requires HX-Request: true on state-changing
    # methods — every real call in the app comes from HTMX, which sets
    # this. TestClient doesn't, so we inject it as a default header for
    # the whole client. See harmonist.web.security.CSRFMiddleware.
    return TestClient(app, headers={"HX-Request": "true"})


def _make_album(cfg, name: str, *, mbid: str | None = None, comment: str | None = None) -> Path:
    d = cfg.paths.music_dir / "Artist" / name
    d.mkdir(parents=True)
    f = d / "01 Track.m4a"
    shutil.copy(SINE_M4A, f)
    if mbid or comment:
        audio = MP4(f)
        if mbid:
            audio[ATOM_MB_ALBUM_ID] = [mbid.encode("utf-8")]
        if comment:
            audio[ATOM_COMMENT] = [comment]
        audio.save()
    return d


def _id_for(cfg, album_dir: Path) -> str:
    """Return the canonical album id (scanner-assigned). Works for NEW
    albums (registry-minted UUID) and sidecar'd albums (UUID or MBID).
    """
    from harmonist import scanner

    for a in scanner.scan(cfg.paths.music_dir):
        if a.path == album_dir:
            return a.id
    raise AssertionError(f"no album at {album_dir}")


# ---------- Bandcamp setup / deferred sync ----------


def test_header_shows_setup_when_no_cookies(client):
    """Fresh install (no cookies) → header offers setup, not sync."""
    r = client.get("/")
    assert r.status_code == 200
    assert "Set up Bandcamp sync" in r.text
    assert "Sync Bandcamp" not in r.text


def test_bandcamp_setup_modal_renders(client):
    r = client.get("/bandcamp/setup")
    assert r.status_code == 200
    assert "cookies.txt" in r.text
    assert "bandcampsync" in r.text  # instructions link
    assert 'hx-post="/bandcamp/cookies"' in r.text


def test_bandcamp_cookies_saved_from_text_flips_header(client, cfg):
    body = "# Netscape HTTP Cookie File\n.bandcamp.com\tTRUE\t/\tFALSE\t0\tident\tabc\n"
    r = client.post("/bandcamp/cookies", data={"cookies_text": body})
    assert r.status_code == 200
    assert r.headers.get("hx-refresh") == "true"
    # Cookies written to the configured path
    assert cfg.cookies_file.exists()
    assert cfg.cookies_file.read_text() == body
    # Header now offers Sync, not setup
    home = client.get("/")
    assert "Sync Bandcamp" in home.text
    assert "Set up Bandcamp sync" not in home.text


def test_bandcamp_cookies_saved_from_upload(client, cfg):
    import io

    body = b"# Netscape HTTP Cookie File\n.bandcamp.com\tTRUE\t/\tFALSE\t0\tident\txyz\n"
    r = client.post(
        "/bandcamp/cookies",
        files={"cookies_file": ("cookies.txt", io.BytesIO(body), "text/plain")},
    )
    assert r.status_code == 200
    assert r.headers.get("hx-refresh") == "true"
    assert cfg.cookies_file.read_bytes() == body


def test_bandcamp_cookies_empty_returns_error_modal(client, cfg):
    r = client.post("/bandcamp/cookies", data={"cookies_text": "   "})
    assert r.status_code == 200
    # No refresh — modal re-rendered with an error, cookies not written
    assert "hx-refresh" not in {k.lower() for k in r.headers}
    assert "Paste your cookies.txt" in r.text
    assert not cfg.cookies_file.exists()


# ---------- basic routes ----------


def test_index_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Inbox" in r.text


def test_index_has_inbox_busy_lock(client):
    """The inbox carries a busy banner (hidden until the /status poll toggles it)
    so a background op dims + locks the inbox."""
    r = client.get("/").text
    assert 'id="inbox-busy"' in r
    assert 'id="inbox-busy-label"' in r
    # Toggled by the status handler on any running op.
    for label in ("Syncing…", "Reconciling…", "Scanning…"):
        assert label in r
    assert "pointer-events-none" in r  # the lock the JS applies to #task-list


def test_tasks_empty_inbox(client):
    r = client.get("/tasks")
    assert r.status_code == 200
    assert "Inbox is empty" in r.text


def test_healthz(client, cfg):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["music_dir"] == str(cfg.paths.music_dir)
    assert body["sync_state"] == "idle"


def test_sync_status_idle(client):
    r = client.get("/sync/status")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "idle"
    assert "current_item" in body  # always present so the JS doesn't NPE


def test_cover_serves_embedded_art_without_folder_cover(client, cfg):
    """No cover.jpg on disk, but the track has embedded art → /cover serves it
    on the fly (200), no need to write it to disk first."""
    from mutagen.mp4 import MP4, MP4Cover

    d = _make_album(cfg, "EmbeddedArt")
    audio = MP4(d / "01 Track.m4a")
    audio["covr"] = [MP4Cover(b"\xff\xd8\xfftest-jpeg-bytes", imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()
    aid = _id_for(cfg, d)

    r = client.get(f"/cover/{aid}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")
    assert b"test-jpeg-bytes" in r.content


def test_inbox_card_omits_cover_request_when_no_cover(client, cfg):
    """A NEW album with no folder cover must NOT emit a /cover <img> — else
    hundreds of them flood the logs with 404s (and fetch even when collapsed)."""
    _make_album(cfg, "NoCoverAlbum")  # no cover.jpg on disk
    r = client.get("/tasks")
    assert 'src="/cover/' not in r.text


def test_inbox_card_lazy_loads_cover_when_present(client, cfg):
    d = _make_album(cfg, "HasCoverAlbum")
    (d / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0")  # looks like a JPEG header
    r = client.get("/tasks")
    assert 'src="/cover/' in r.text
    assert 'loading="lazy"' in r.text


def test_inbox_card_shows_the_path_relative_to_the_library(client, cfg):
    """Under Docker the library prefix is a container path that means nothing to
    the user, and the tail is what identifies the album — the reason audit
    records went relative in #98. The inbox card was the last place still
    printing it in full (#121). Absolute stays available on hover."""
    d = _make_album(cfg, "RelativeMe")
    body = client.get("/tasks").text

    assert f'title="{d}"' in body  # absolute, one hover away
    rel = str(d.relative_to(cfg.paths.music_dir))
    assert f">{rel}</p>" in body
    # The visible text must not be the absolute path. Checked on the rendered
    # <p> rather than the whole page, since `title` legitimately carries it.
    assert f">{d}</p>" not in body


def test_no_template_renders_a_bare_library_path(cfg):
    """A guard for the whole class, not just the card #121 fixed: `{{ album.path }}`
    is the absolute path, and every place showing it to the user wants
    `rel_path(...)`. Cheap to keep, and it fails on the next one added."""
    import re
    from pathlib import Path

    offenders = []
    for tpl in (Path(__file__).resolve().parents[1] / "templates").rglob("*.html"):
        for n, line in enumerate(tpl.read_text().splitlines(), 1):
            # `{{ album.path }}` used as content. A `title="{{ album.path }}"`
            # attribute is the intended escape hatch and is exempt.
            if re.search(r"(?<!title=\")\{\{\s*album\.path\s*\}\}(?!\")", line):
                offenders.append(f"{tpl.name}:{n}")
    assert not offenders, f"render these via rel_path(...): {offenders}"


def test_consolidated_status_endpoint(client):
    """One poll returns sync + reconcile + scan + counts, replacing separate polls."""
    r = client.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"sync", "reconcile", "scan", "counts", "pending"}
    assert body["sync"]["state"] == "idle"
    assert body["reconcile"]["state"] == "idle"
    assert "state" in body["scan"]
    # Single source of truth for the inbox/library numbers.
    for key in ("inbox", "library", "new", "needs_sync"):
        assert key in body["counts"]


def test_tasks_renders_inbox_count(client, cfg):
    """The inbox count lives inside the polled /tasks fragment so it
    updates without a full-page reload.
    """
    _make_album(cfg, "New Album")
    _make_album(cfg, "Another One")
    r = client.get("/tasks")
    assert r.status_code == 200
    assert "2" in r.text
    assert "need attention" in r.text


def test_tasks_does_not_render_library_total(client, cfg):
    """The Inbox header is for inbox concerns; library-wide totals belong
    to a separate Library box (TBD). Regression on the user's UX feedback.
    """
    _make_album(cfg, "X")
    r = client.get("/tasks")
    assert "total in library" not in r.text


def test_tasks_groups_albums_by_state_with_headers_and_instructions(client, cfg):
    """Each state appears as its own <section> with a heading + instruction line."""
    # NEEDS_MBID with store_url
    d1 = _make_album(cfg, "WithURL")
    sc.write(
        d1,
        Sidecar(
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=1),
        ),
    )
    # NEEDS_MBID without store_url
    d2 = _make_album(cfg, "Manual")
    sc.write(d2, Sidecar())

    r = client.get("/tasks")
    # Both are NEEDS_MBID — single section heading
    assert ">MBID</abbr>" in r.text  # the "Needs MBID" header (MBID is an <abbr>)
    # Open-in-Harmony link appears for the store_url card
    assert "Open in Harmony" in r.text
    # Manual MBID form appears too
    assert 'name="mbid"' in r.text


def test_tasks_state_group_omitted_when_empty(client, cfg):
    """No section header rendered for states without any albums."""
    _make_album(cfg, "Only New")
    r = client.get("/tasks")
    assert "New" in r.text
    # No NEEDS_MBID / NEEDS_REVIEW / NEEDS_SYNC headers. Match a fragment unique
    # to the NEEDS_MBID header (the New header now also mentions MBID).
    assert "No confirmed MusicBrainz release" not in r.text
    assert "Needs Review" not in r.text
    assert "Needs Link" not in r.text


def test_tasks_needs_sync_section_advises_sync(client, cfg):
    """The Needs Link group instructions point the user to click Sync."""
    d = _make_album(cfg, "UB")
    audio = MP4(d / "01 Track.m4a")
    audio[ATOM_MB_ALBUM_ID] = [b"rel-a"]
    audio.save()
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=None),
            mb_release_id="rel-a",
            tagged_at=datetime.now(UTC),
        ),
    )
    r = client.get("/tasks")
    assert "Needs Link" in r.text
    # Instruction points at the (header) Sync button — there's no inline
    # bulk-sync button; the header Sync popover is the single entry point.
    assert "Click Sync" in r.text
    assert "Sync to link" not in r.text


def test_large_inbox_group_collapses_into_details(client, cfg):
    """A group past the collapse threshold (12) folds into a <details> so a
    big library import doesn't render as an endless wall of cards."""
    for i in range(13):
        _make_album(cfg, f"New {i:02d}")
    r = client.get("/tasks")
    assert "Show all 13 albums" in r.text


def test_small_inbox_group_not_collapsed(client, cfg):
    """Below the threshold the cards render inline — no 'Show all' disclosure.
    (Cards carry their own <details> for tools, so 'Show all N' is the marker.)"""
    _make_album(cfg, "Solo New")
    r = client.get("/tasks")
    assert "Show all" not in r.text


def test_tasks_empty_state_message_distinguishes_zero_vs_all_done(client, cfg):
    # Empty library
    r = client.get("/tasks")
    assert "Drop some albums" in r.text or "Inbox is empty" in r.text


# ---------- state dispatch — each card type renders ----------


def test_new_card_rendered(client, cfg):
    _make_album(cfg, "New Album")  # untagged
    r = client.get("/tasks")
    assert "New" in r.text
    assert "need a MusicBrainz release" in r.text  # group header states what's needed
    assert 'id="newtools-' in r.text  # search/paste tools present


def test_needs_mbid_card_with_store_url_rendered(client, cfg):
    d = _make_album(cfg, "HasURL")
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=1),
        ),
    )
    r = client.get("/tasks")
    assert ">MBID</abbr>" in r.text  # the "Needs MBID" header (MBID is an <abbr>)
    assert "Open in Harmony" in r.text
    assert "harmony.pulsewidth.org.uk" in r.text


def test_needs_mbid_private_release_suppresses_harmony_and_recheck(client, cfg):
    """A private Bandcamp release (public URL 404s) must not offer Harmony
    seeding or Recheck — its URL can't be added to MusicBrainz. The manual
    MBID form stays, and the badge reads 'Bandcamp (private)'."""
    d = _make_album(cfg, "Private")
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/secret",
            bandcamp=BandcampInfo(item_id=1, is_private=True),
        ),
    )
    r = client.get("/tasks")
    assert ">MBID</abbr>" in r.text  # still a Needs MBID card
    assert "Open in Harmony" not in r.text
    assert "/recheck/" not in r.text
    assert "won't resolve on MusicBrainz" in r.text
    assert "Bandcamp (private)" in r.text
    # No store-URL search mode for a private release — only name search.
    assert "Look up releases at this URL" not in r.text
    assert "Search MusicBrainz by name" in r.text
    # The manual resolution path remains.
    assert 'name="mbid"' in r.text


def test_needs_mbid_card_without_store_url_rendered(client, cfg):
    d = _make_album(cfg, "NoURL")
    sc.write(d, Sidecar())
    r = client.get("/tasks")
    assert ">MBID</abbr>" in r.text  # the "Needs MBID" header (MBID is an <abbr>)
    # Manual MBID form is included
    assert 'name="mbid"' in r.text
    assert "/manual/" in r.text


def test_new_card_untagged_offers_search_no_reconcile(client, cfg):
    _make_album(cfg, "New Album")  # untagged: no MB Album Id atom
    r = client.get("/tasks")
    # The group header states what's needed; the card is just the search/paste
    # tools — no per-card blurb, and no (useless here) "Reconcile from tags".
    assert "need a MusicBrainz release" in r.text
    assert "Reconcile from tags" not in r.text
    assert "Assign &amp; Tag" in r.text or "Assign & Tag" in r.text
    assert "Recover store URL" not in r.text  # URL recovery is automatic now


def test_new_card_tagged_orphan_offers_reconcile(client, cfg):
    _make_album(cfg, "Orphan", mbid="rel-xyz")  # MB Album Id atom, no sidecar
    r = client.get("/tasks")
    assert "Reconcile from tags" in r.text


def test_needs_review_card_renders_side_by_side(client, cfg):
    d = _make_album(cfg, "NR Album")
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=1),
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-aaa",
                confidence="approximate",
                file_count=2,
                track_count=2,
                track_comparisons=[
                    TrackComparison(
                        file_name="01.m4a",
                        file_duration_ms=180000,
                        file_title="Side A",
                        mb_track_title="Side A",
                        mb_track_length_ms=185000,
                        delta_ms=5000,
                    ),
                    TrackComparison(
                        file_name="02.m4a",
                        file_duration_ms=200000,
                        file_title="Side B",
                        mb_track_title="Side B",
                        mb_track_length_ms=200500,
                        delta_ms=500,
                    ),
                ],
            ),
        ),
    )
    r = client.get("/tasks")
    # Merged into NEEDS_MBID: the card shows the suggestion side-by-side inline.
    assert ">MBID</abbr>" in r.text  # the "Needs MBID" header (MBID is an <abbr>)
    assert "MusicBrainz suggests" in r.text
    assert "approximate" in r.text
    assert "Side A" in r.text
    assert "Confirm" in r.text
    assert "Dismiss suggestion" in r.text
    # Re-query MB for the same release (after fixing it upstream)
    assert "Refresh from MB" in r.text


def test_mistag_card_renders_open_panel_with_sibling_explanation(client, cfg):
    """A mis-tag candidate renders the 'Wrong release?' panel OPEN with a
    store-URL-based sibling explanation, kept separate from the track-count
    match-quality note."""
    d = _make_album(cfg, "Mistag Card")
    sc.write(
        d,
        Sidecar(
            store_url="https://ultimae.bandcamp.com/album/life",
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-correct",
                confidence="no_match",
                file_count=10,
                track_count=11,
                notes=["file count 10 does not match MB track count 11"],
                mistag_owned_url="https://ultimae.bandcamp.com/album/life",
                mistag_owned_label="ASURA / Life²",
                mistag_owned_disambig="24-bit",
                mistag_tagged_mbid="rel-wrong",
                mistag_tagged_label="ASURA / Life²",
                mistag_tagged_disambig="",
                mistag_release_group_mbid="rg-life",
            ),
        ),
    )
    r = client.get("/tasks")
    # The wrong-release panel is rendered OPEN and names both releases.
    assert "<details open" in r.text
    assert "Wrong release?" in r.text
    assert "ASURA / Life²" in r.text
    # Both releases link to MusicBrainz (tagged + owned/suggested), and the
    # shared release group links too.
    assert "https://musicbrainz.org/release/rel-wrong" in r.text
    assert "https://musicbrainz.org/release/rel-correct" in r.text
    assert "https://musicbrainz.org/release-group/rg-life" in r.text
    # The store URL is hidden behind the word "Bandcamp", not shown raw.
    assert ">Bandcamp</a>" in r.text
    # Disambiguation is rendered in parentheses, visually distinct (muted span).
    assert ">(24-bit)</span>" in r.text
    # The track-count note stays in the match-quality box, NOT in the mis-tag
    # prose, and is flagged with a "Note:" prefix.
    assert "Note:" in r.text
    assert "file count 10 does not match MB track count 11" in r.text
    # File count (10) < track count (11): the only valid tag is incomplete, so
    # that's the primary action — no misleading highlighted "Confirm & Tag".
    assert "Confirm as Incomplete (10 of 11)" in r.text
    assert "Confirm &amp; Tag" not in r.text


def test_mistag_renders_in_own_top_level_section(client, cfg):
    """A mis-tag (candidate carries mistag_owned_url) is lifted into its own
    top-level 'Likely mis-tagged' section ABOVE Needs MBID, while a plain
    NEEDS_MBID album stays under the Needs MBID header. Both remain
    AlbumState.NEEDS_MBID — this is a rendering split, not a new state."""
    mt = _make_album(cfg, "Mistagged One")
    sc.write(
        mt,
        Sidecar(
            store_url="https://ultimae.bandcamp.com/album/life",
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-correct",
                confidence="no_match",
                file_count=10,
                track_count=10,
                mistag_owned_url="https://ultimae.bandcamp.com/album/life",
                mistag_owned_label="ASURA / Life²",
                mistag_tagged_mbid="rel-wrong",
                mistag_tagged_label="ASURA / Life²",
                mistag_release_group_mbid="rg-life",
            ),
        ),
    )
    # A plain NEEDS_MBID album (sidecar, no MBID, no candidate).
    plain = _make_album(cfg, "Plain Needs Mbid")
    sc.write(plain, Sidecar())

    r = client.get("/tasks")
    body = r.text
    assert "Possibly mis-tagged" in body
    header = body.index("Needs <abbr")  # the Needs MBID group header
    # The mis-tag section is above the Needs MBID group.
    assert body.index("Possibly mis-tagged") < header
    # The mis-tagged album sits in the mis-tag section (before Needs MBID); the
    # plain one sits under Needs MBID (after it).
    assert body.index("Mistagged One") < header
    assert body.index("Plain Needs Mbid") > header


def test_surrender_card_renders_readonly_with_tools(client, cfg):
    """A surrender candidate (unmatched_purchase, no matching download) leads with
    'Move to Library' + the withdrawn-release explanation, and offers the 'wrong
    release?' escape hatch — but NO Confirm & Tag (that would loop to Needs Link)."""
    d = _make_album(cfg, "Surrendered")
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/surrendered",
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-surr",
                confidence="exact",
                file_count=10,
                track_count=10,
                unmatched_purchase=True,
            ),
        ),
    )
    from harmonist import scanner

    a = next(x for x in scanner.scan(cfg.paths.music_dir) if x.path == d)
    r = client.get("/tasks")
    assert "No matching Bandcamp purchase" in r.text
    assert "withdrawn from Bandcamp" in r.text  # names the common cause
    # Primary action is Move to Library; no Confirm-and-tag loop.
    assert f"/surrender/{a.id}/keep" in r.text
    assert "Move to Library" in r.text
    assert "Confirm &amp; Tag" not in r.text
    assert "/confirm/" not in r.text
    # The redundant store-URL lookup is gone; the name/MBID escape hatch remains.
    assert "Look up releases at this URL" not in r.text


def test_surrender_is_audited(client, cfg):
    """#88: a surrender is named in the review gate. Setting the MBID from the
    candidate was already audited as an identity change, but
    `purchase_unavailable` — the defining, PERMANENT part of the decision — moved
    silently. The scanner then treats the album as terminal forever."""
    from harmonist import activity_store, scanner
    from harmonist.activity_store import Source

    d = _make_album(cfg, "Surrendered", mbid="rel-sur")
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/sur",
            mb_release_id=None,
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-sur",
                confidence="exact",
                file_count=1,
                track_count=1,
                unmatched_purchase=True,
            ),
        ),
    )
    a = next(x for x in scanner.scan(cfg.paths.music_dir) if x.path == d)
    activity_store.clear()

    assert client.post(f"/surrender/{a.id}/keep").status_code == 200

    rows = [e.message for e in activity_store.recent(50, source=Source.AUDIT)]
    upd = next(m for m in rows if m.startswith("sidecar.update"))
    assert "purchase_unavailable=False->True" in upd


def test_surrender_keep_marks_purchase_unavailable_and_completes(client, cfg):
    """'Move to Library' restores the release id from the surrender candidate, flags
    the purchase unavailable, and the album classifies COMPLETE — and STAYS complete
    (a re-scan won't drag it back to NEEDS_SYNC despite the Bandcamp store_url)."""
    from harmonist import scanner
    from harmonist.models import AlbumState

    d = _make_album(cfg, "Withdrawn", mbid="rel-wd")  # files tagged with rel-wd
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/withdrawn",
            mb_release_id=None,
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-wd",
                confidence="exact",
                file_count=1,
                track_count=1,
                unmatched_purchase=True,
            ),
        ),
    )
    a = next(x for x in scanner.scan(cfg.paths.music_dir) if x.path == d)
    assert a.state == AlbumState.NEEDS_MBID

    r = client.post(f"/surrender/{a.id}/keep")
    assert r.status_code == 200, r.text

    loaded = sc.read(d)
    assert loaded.purchase_unavailable is True
    assert loaded.mb_release_id == "rel-wd"
    assert loaded.mb_match_candidate is None
    after = next(x for x in scanner.scan(cfg.paths.music_dir) if x.path == d)
    assert after.state == AlbumState.COMPLETE  # terminal, despite the bandcamp store_url


def test_inconsistent_card_renders(client, cfg):
    """A dir with conflicting album tags surfaces an INCONSISTENT card,
    showing the per-file table and the Picard nudge."""
    d = _make_album(cfg, "Mixed")
    # _make_album wrote one track; add a second with a conflicting album tag.
    second = d / "02 Another.m4a"
    shutil.copy(SINE_M4A, second)
    a1 = MP4(d / "01 Track.m4a")
    a1[ATOM_ALBUM] = ["Album A"]
    a1.save()
    a2 = MP4(second)
    a2[ATOM_ALBUM] = ["Album B"]
    a2.save()

    r = client.get("/tasks")
    assert r.status_code == 200
    assert "Inconsistent" in r.text
    assert "Picard" in r.text
    # The per-file table should list both conflicting titles
    assert "Album A" in r.text
    assert "Album B" in r.text


def test_needs_sync_card_renders(client, cfg):
    d = _make_album(cfg, "UB Album")
    # Tag the file so scanner sees it as DONE-style (mb_release_id matches)
    audio = MP4(d / "01 Track.m4a")
    audio[ATOM_MB_ALBUM_ID] = [b"rel-aaa"]
    audio.save()
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=None),
            mb_release_id="rel-aaa",
            tagged_at=datetime.now(UTC),
        ),
    )
    r = client.get("/tasks")
    assert "Needs Link" in r.text
    assert "Mark purchased elsewhere" in r.text
    assert 'hx-post="/unconfirmed/' in r.text
    # URL input is pre-filled with the existing URL (not just a placeholder)
    assert 'value="https://x.bandcamp.com/album/y"' in r.text


# ---------- action endpoints ----------


def test_post_sync_starts_runner(client):
    # Replace the runner_fn to a no-op so we don't try to hit Bandcamp
    runner = client.app.state.sync_runner
    runner._runner_fn = lambda: None
    r = client.post("/sync")
    assert r.status_code == 200
    assert "Sync started" in r.text


def test_sync_route_does_not_duplicate_the_start_entry(client):
    """#101: starting a sync wrote TWO feed entries — the route's flash and the
    runner's generic line — and neither said whether it was link-only. The route
    keeps its status-bar flash but no longer writes an entry; the runner writes
    the single authoritative one once it knows the mode."""
    import time

    from harmonist import activity

    runner = client.app.state.sync_runner
    runner._runner_fn = lambda: None
    activity.clear()

    r = client.post("/sync")
    assert r.status_code == 200
    assert "Sync started" in r.text  # the status-bar flash still fires

    for _ in range(50):  # let the runner thread finish
        if runner.status()["state"] == "idle":
            break
        time.sleep(0.02)

    starts = [e.message for e in activity.recent(20) if "ync started" in e.message]
    assert starts == [], f"the route should write no start entry, got {starts}"


def test_post_sync_409_when_already_running(client):
    runner = client.app.state.sync_runner
    # Manually flag as running so the second POST hits the 409 branch
    runner._status.state = "running"
    r = client.post("/sync")
    assert r.status_code == 409
    assert "already running" in r.text


def test_post_sync_409_while_reconciling(client):
    # A sync must not start while a reconcile pass is in flight — it mutates
    # sidecars and the inbox. The endpoint backstops the disabled button.
    client.app.state.sync_runner._runner_fn = lambda: None
    client.app.state.reconcile_runner._status.state = "running"
    r = client.post("/sync")
    assert r.status_code == 409
    assert "reconciling" in r.text.lower()


def test_post_sync_409_during_the_cold_start_scan(client):
    # Until the first scan lands there's no snapshot, so a sync would run
    # against a library it can't see. The button is disabled client-side; this
    # is the backstop for a stale page or the window before the next poll.
    client.app.state.sync_runner._runner_fn = lambda: None
    client.app.state.scan_runner._status.state = "scanning"
    client.app.state.scan_runner._status.seq = 0
    r = client.post("/sync")
    assert r.status_code == 409
    assert "scanning" in r.text.lower()


def test_post_sync_allowed_during_a_later_rescan(client):
    # Only the FIRST scan blocks: a later rescan (seq > 0) has a usable
    # snapshot behind it, so it must not lock the user out of syncing.
    client.app.state.sync_runner._runner_fn = lambda: None
    client.app.state.scan_runner._status.state = "scanning"
    client.app.state.scan_runner._status.seq = 3
    assert client.post("/sync").status_code == 200


def test_needs_sync_bulk_button_absent_while_reconciling(client, cfg):
    # During reconcile the inbox renders the live-count panel, not the groups —
    # so the bulk-sync button isn't offered at all (can't race the pass).
    d = _make_album(cfg, "BulkSync")
    audio = MP4(d / "01 Track.m4a")
    audio[ATOM_MB_ALBUM_ID] = [b"rel-bulk-1"]
    audio.save()
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=None),
            mb_release_id="rel-bulk-1",
            tagged_at=datetime.now(UTC),
        ),
    )
    client.app.state.reconcile_runner._status.state = "running"
    r = client.get("/tasks")
    assert r.status_code == 200
    assert "Sorting your library" in r.text  # the reconcile panel
    assert "sync-trigger" not in r.text  # ...no bulk-sync button


def test_needs_sync_group_collapses_to_progress_note_while_syncing(client, cfg):
    # While a sync is in flight the NEEDS_SYNC group hides its bulk button and
    # its "Show all N albums" card list, showing a progress note instead — the
    # albums are being linked right now and clear when the sync finishes.
    d = _make_album(cfg, "BulkSyncBusy")
    audio = MP4(d / "01 Track.m4a")
    audio[ATOM_MB_ALBUM_ID] = [b"rel-bulk-3"]
    audio.save()
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=None),
            mb_release_id="rel-bulk-3",
            tagged_at=datetime.now(UTC),
        ),
    )
    client.app.state.sync_runner._status.state = "running"
    r = client.get("/tasks")
    assert r.status_code == 200
    assert "Linking these to your Bandcamp purchases" in r.text
    assert "sync-trigger" not in r.text  # bulk button gone while syncing
    assert "Show all" not in r.text  # no expandable card list either


def test_tasks_shows_live_reconcile_counts(client, cfg):
    """During reconcile the inbox shows the live base+tally counts (need
    attention = reconcile.inbox, and a New/Needs-Sync/Library split building
    up) rather than the frozen snapshot — and NOT the status bar's X/Y."""
    _make_album(cfg, "Fresh")  # a NEW orphan (frozen snapshot would say 1)
    runner = client.app.state.reconcile_runner
    runner._status.state = "running"
    runner._status.completed = 143
    runner._status.total = 346
    runner._status.inbox = 60
    runner._status.new = 5
    runner._status.needs_sync = 55
    runner._status.library = 78
    r = client.get("/tasks")
    assert r.status_code == 200
    assert "Sorting your library" in r.text  # the live-count panel
    # In-page "need attention" tracks reconcile.inbox (the tab badge itself now
    # comes from /status `counts`, tested separately).
    assert ">60</span> need attention" in r.text
    assert ">5</span> New" in r.text  # the building split
    assert ">55</span> Needs Link" in r.text
    assert ">78</span> to Library" in r.text
    assert "143 / 346" not in r.text  # progress lives in the status bar
    assert "Fresh" not in r.text  # frozen card list not rendered


def test_tasks_kicks_reconcile_only_for_reconcilable_orphan(client, cfg):
    """A NEW album with an MBID atom (reconcilable) kicks reconcile; an
    untagged orphan does not — so incidental inbox refreshes don't churn."""
    started: list[bool] = []

    def fake_start() -> bool:
        started.append(True)
        return True

    client.app.state.reconcile_runner.start = fake_start

    _make_album(cfg, "Untagged")  # NEW, no MBID atom → not reconcilable
    client.get("/tasks")
    assert started == []  # untagged orphan must NOT kick reconcile

    _make_album(cfg, "Tagged", mbid="rel-z")  # NEW + MBID atom → reconcilable
    client.get("/tasks")
    assert started == [True]  # now there's something reconcile can resolve


def test_tasks_skips_reconcile_for_forgotten_orphan(client, cfg):
    """A Forgotten (exempt) MBID-tagged orphan must not kick reconcile — the
    runner would skip it anyway, so kicking would loop forever."""
    started: list[bool] = []

    def fake_start() -> bool:
        started.append(True)
        return True

    client.app.state.reconcile_runner.start = fake_start

    d = _make_album(cfg, "Forgotten", mbid="rel-f")  # NEW + MBID atom
    client.app.state.forgotten_paths.add(d)
    client.get("/tasks")
    assert started == []


def test_tasks_hides_groups_and_shows_panel_while_reconciling(client, cfg):
    """While reconcile runs, the inbox shows the live-count panel instead of the
    (frozen, re-polled) group card lists."""
    _make_album(cfg, "FreshOne")  # NEW orphan; frozen snapshot would render it
    runner = client.app.state.reconcile_runner
    runner._status.state = "running"
    runner._status.new = 3
    r = client.get("/tasks")
    assert r.status_code == 200
    assert "Sorting your library" in r.text  # the live-count panel
    assert ">3</span> New" in r.text  # the building split
    assert "FreshOne" not in r.text  # no card rendered
    assert "Show all" not in r.text  # ...and no disclosure


def test_tasks_new_group_shows_cards_when_idle(client, cfg):
    """Sanity counterpart: with no reconcile running the NEW card renders."""
    _make_album(cfg, "FreshTwo")
    r = client.get("/tasks")
    assert r.status_code == 200
    assert "FreshTwo" in r.text


def test_tasks_no_group_cards_while_reconciling(client, cfg):
    """No group cards render during reconcile (the panel replaces them) — e.g.
    the Needs Link card's actions are gone."""
    _needs_sync_album(cfg, "Linkme", "rel-q")
    client.app.state.reconcile_runner._status.state = "running"
    r = client.get("/tasks")
    assert r.status_code == 200
    assert "Sorting your library" in r.text  # the panel
    assert "Mark purchased elsewhere" not in r.text  # ...not the needs-sync card


def test_tasks_needs_sync_card_shown_when_idle(client, cfg):
    """Sanity counterpart: idle → the Needs Link card renders normally."""
    _needs_sync_album(cfg, "Linkme2", "rel-r")
    r = client.get("/tasks")
    assert r.status_code == 200
    assert "Mark purchased elsewhere" in r.text


def test_tasks_in_page_attention_line_counts_inbox(client, cfg):
    """The in-page 'N need attention' line reflects the snapshot when idle.
    (The tab badges themselves come from /status `counts` — tested separately.)"""
    _make_album(cfg, "InboxA")
    _make_album(cfg, "InboxB")
    r = client.get("/tasks")
    assert "need attention" in r.text
    assert "2" in r.text

    runner = client.app.state.reconcile_runner
    runner._status.state = "running"
    runner._status.inbox = 7  # base + tallies so far
    r = client.get("/tasks")
    assert "7" in r.text  # running → reconcile.inbox
    assert "InboxA" not in r.text  # ...and no cards


def _needs_sync_album(cfg, name: str, mbid: str) -> Path:
    """A tagged, on-disk album with a Bandcamp store_url but no item_id →
    scans as NEEDS_SYNC (the post-sync audit's input set)."""
    d = _make_album(cfg, name, mbid=mbid)
    sc.write(
        d,
        Sidecar(
            store_url=f"https://label.bandcamp.com/album/{name.lower()}",
            bandcamp=BandcampInfo(item_id=None),
            mb_release_id=mbid,
            tagged_at=datetime.now(UTC),
        ),
    )
    return d


def test_report_unmatched_partial_sync_warns_but_does_not_demote(cfg):
    """On a PARTIAL sync (checkpoint-limited) we only warn — the purchase may be
    below the checkpoint. The album stays NEEDS_SYNC; nothing is demoted."""
    from harmonist import activity, scanner
    from harmonist.models import AlbumState
    from harmonist.web.main import _report_unmatched_after_sync

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    d1 = _needs_sync_album(cfg, "Stranded", "rel-Stranded")
    _needs_sync_album(cfg, "Adrift", "rel-Adrift")
    activity.clear()
    _report_unmatched_after_sync(cfg, full_sync=False)
    warnings = [e for e in activity.recent(10) if e.level == "warning"]
    assert len(warnings) == 2
    assert all("Not linked to a Bandcamp purchase" in w.message for w in warnings)
    assert all("Try a different URL" in w.message for w in warnings)
    msgs = " ".join(w.message for w in warnings)
    assert "https://label.bandcamp.com/album/stranded" in msgs
    # Still NEEDS_SYNC — not demoted.
    album = next(a for a in scanner.scan(cfg.paths.music_dir) if a.path == d1)
    assert album.state == AlbumState.NEEDS_SYNC


def test_report_unmatched_full_sync_surrenders_to_needs_mbid(cfg):
    """On a FULL sync, an album with no matching purchase is dropped back to
    NEEDS_MBID with its current release kept as a read-only suggestion."""
    from harmonist import activity, scanner
    from harmonist.models import AlbumState
    from harmonist.web.main import _report_unmatched_after_sync

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    d = _needs_sync_album(cfg, "Orphan", "rel-orphan")
    activity.clear()
    _report_unmatched_after_sync(cfg, full_sync=True)
    loaded = sc.read(d)
    assert loaded.mb_release_id is None  # demoted out of NEEDS_SYNC
    cand = loaded.mb_match_candidate
    assert cand is not None
    assert cand.mb_release_id == "rel-orphan"  # current release kept as suggestion
    assert cand.unmatched_purchase is True
    album = next(a for a in scanner.scan(cfg.paths.music_dir) if a.path == d)
    assert album.state == AlbumState.NEEDS_MBID
    msgs = [e.message for e in activity.recent(10)]
    assert any(
        "No Bandcamp purchase matched" in m and "Needs MBID" in m and "duplicate" in m for m in msgs
    )


def test_surrender_leaves_on_disk_file_tags_intact(cfg):
    """Surrender only rewrites the sidecar — it must NOT untag the audio files.
    This is what makes it non-destructive: the release is still written on disk
    and is offered as a one-click re-confirm suggestion. (Pins the deferred
    user-assigned-MBID behavior: a manually-assigned tag is re-inboxed, never
    erased — see design.md §3.)"""
    from harmonist import formats
    from harmonist.web.main import _report_unmatched_after_sync

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    d = _needs_sync_album(cfg, "Orphan", "rel-orphan")
    track = d / "01 Track.m4a"
    assert formats.read_album_id(track) == "rel-orphan"  # tagged before surrender

    _report_unmatched_after_sync(cfg, full_sync=True)

    # Sidecar demoted, but the file's MB Album Id atom is untouched.
    assert sc.read(d).mb_release_id is None
    assert formats.read_album_id(track) == "rel-orphan"


def test_surrender_flags_possible_duplicate_of_linked_album(cfg):
    """When a surrendered album is tagged as the SAME release as one already
    linked to a purchase, log a non-committal 'possible duplicate / split'
    warning (we don't auto-resolve — a release can legitimately span dirs)."""
    from harmonist import activity
    from harmonist.web.main import _report_unmatched_after_sync

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    # The linked copy (has an item_id) — COMPLETE.
    linked = _make_album(cfg, "LinkedCopy", mbid="rel-dup")
    sc.write(
        linked,
        Sidecar(
            store_url="https://x.bandcamp.com/album/dup",
            bandcamp=BandcampInfo(item_id=999),
            mb_release_id="rel-dup",
            tagged_at=datetime.now(UTC),
        ),
    )
    # The unlinked twin tagged as the same release → NEEDS_SYNC → surrenders.
    _needs_sync_album(cfg, "DupCopy", "rel-dup")
    activity.clear()
    _report_unmatched_after_sync(cfg, full_sync=True)
    msgs = [e.message for e in activity.recent(10)]
    assert any("possibly a duplicate copy" in m or "duplicate copy" in m for m in msgs)


def test_report_unmatched_after_sync_quiet_when_all_linked(cfg):
    """A linked album (item_id set) is COMPLETE, not NEEDS_SYNC — no warning."""
    from harmonist import activity
    from harmonist.web.main import _report_unmatched_after_sync

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    d = _make_album(cfg, "Linked", mbid="rel-y")
    sc.write(
        d,
        Sidecar(
            store_url="https://label.bandcamp.com/album/linked",
            bandcamp=BandcampInfo(item_id=123),
            mb_release_id="rel-y",
            tagged_at=datetime.now(UTC),
        ),
    )
    activity.clear()
    _report_unmatched_after_sync(cfg, full_sync=True)
    assert [e for e in activity.recent(10) if e.level == "warning"] == []


def test_force_full_sync_clears_checkpoint_when_pending_links(cfg, client):
    """A NEEDS_SYNC album means a purchase still needs linking — clear the
    checkpoint so the next sync re-pages the whole collection."""
    from harmonist.web.main import _BANDCAMPSYNC_STATE_FILE, _force_full_sync_if_pending_links

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    _needs_sync_album(cfg, "Pending", "rel-pending")
    state = cfg.paths.music_dir / _BANDCAMPSYNC_STATE_FILE
    state.write_text("{}")
    pending = _force_full_sync_if_pending_links(cfg, client.app.state.scan_runner)
    assert not state.exists()  # cleared → next sync is full
    assert pending == 1  # the gate signal: >0 → run the sync link-only


def test_configure_logging_quiets_noisy_bandcampsync_loggers(cfg):
    """bandcampsync floods every sync with per-item lines on its own loggers —
    'ignores' (WARNING, normal already-downloaded) and 'sync' (INFO, progress).
    At INFO we raise their thresholds; genuine third-party errors still surface."""
    import logging

    from harmonist.web.main import _configure_logging

    for name in ("ignores", "sync"):
        logging.getLogger(name).setLevel(logging.NOTSET)
    _configure_logging(cfg)  # default log_level == "info"
    assert logging.getLogger("ignores").level == logging.ERROR
    assert logging.getLogger("sync").level == logging.WARNING


def test_configure_logging_leaves_thirdparty_verbose_at_debug(cfg):
    """At DEBUG we leave the third-party loggers verbose for deep debugging."""
    import logging

    from harmonist.web.main import _configure_logging

    logging.getLogger("ignores").setLevel(logging.NOTSET)
    cfg.log_level = "debug"
    _configure_logging(cfg)
    assert logging.getLogger("ignores").level != logging.ERROR


def test_force_full_sync_keeps_checkpoint_when_nothing_pending(cfg, client):
    """No NEEDS_SYNC albums → the checkpoint is left intact (fast incremental)."""
    from harmonist.web.main import _BANDCAMPSYNC_STATE_FILE, _force_full_sync_if_pending_links

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)  # empty library
    state = cfg.paths.music_dir / _BANDCAMPSYNC_STATE_FILE
    state.write_text("{}")
    pending = _force_full_sync_if_pending_links(cfg, client.app.state.scan_runner)
    assert state.exists()  # untouched
    assert pending == 0  # nothing pending → downloads enabled (not link-only)


# ---------- mis-tag detection via release-group join ----------


def _release_with_rg(
    mbid: str,
    rg: str,
    n_tracks: int = 1,
    *,
    artist: str = "",
    title: str = "Album",
    disambiguation: str = "",
) -> dict:
    tracks = [
        {
            "id": f"rt-{i}",
            "position": str(i),
            "title": f"T{i}",
            "recording": {"id": f"rec-{i}", "title": f"T{i}", "length": "1000"},
        }
        for i in range(1, n_tracks + 1)
    ]
    rel: dict = {
        "id": mbid,
        "title": title,
        "release-group": {"id": rg, "title": "RG"},
        "medium-list": [{"position": "1", "track-list": tracks}],
    }
    if artist:
        rel["artist-credit-phrase"] = artist
    if disambiguation:
        rel["disambiguation"] = disambiguation
    return rel


class _FakeSyncer:
    def __init__(self, unmatched):
        self._unmatched = unmatched

    def unmatched_purchases(self):
        return self._unmatched


def test_detect_mistag_by_release_group_demotes_with_suggestion(cfg):
    """An unmatched-after-sync album whose release group contains a *different*
    edition the user OWNS (a purchase that linked to no album) → mis-tag: demote
    to NEEDS_MBID with that owned edition suggested, naming the purchase.

    Critically: the 270-style noise (purchases NOT in the album's release group)
    costs nothing — detection is driven by the album + one release-group browse."""
    from harmonist import activity, scanner
    from harmonist.models import AlbumState
    from harmonist.web.main import _detect_mistags_after_sync

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    d = _needs_sync_album(cfg, "Mistag", "rel-wrong")
    # One relevant purchase (the owned 24-bit edition) buried in lots of noise.
    owned = [(i, f"https://noise.bandcamp.com/album/n{i}", f"Noise / {i}") for i in range(250)]
    owned.append(
        (9999, "https://ultimae.bandcamp.com/album/live-in-corfu-24bit", "Cell / Live in Corfu")
    )
    syncer = _FakeSyncer(owned)

    # rel-wrong's release group rg-1 contains the tagged edition and the owned one.
    def browse_rg(rg):
        assert rg == "rg-1"
        return [
            ("rel-wrong", ["https://ultimae.bandcamp.com/album/live-in-corfu"]),
            ("rel-correct", ["https://ultimae.bandcamp.com/album/live-in-corfu-24bit"]),
        ]

    # Distinct MB releases for the tagged (standard) vs owned (24-bit) editions
    # so we can assert both names AND the disambiguation are captured.
    def fetch_release(m):
        disambig = "24-bit" if m == "rel-correct" else ""
        return _release_with_rg(
            m, "rg-1", artist="Cell", title="Live in Corfu", disambiguation=disambig
        )

    # The sync already recorded these as potential downloads (replace_all runs
    # during the sync, before detection): the owned edition + unrelated noise.
    from harmonist import pending_downloads as pd

    pd.replace_all(
        [
            _pp(9999, url="https://ultimae.bandcamp.com/album/live-in-corfu-24bit"),
            _pp(9998, url="https://noise.bandcamp.com/album/n1"),
        ]
    )

    activity.clear()
    _detect_mistags_after_sync(
        cfg,
        syncer,
        browse_rg=browse_rg,
        fetch_release=fetch_release,
    )
    # The mis-tag claims its purchase out of the potential-downloads list (it's
    # now represented by the mis-tag card); unrelated pending is untouched.
    assert pd.get(9999) is None
    assert pd.get(9998) is not None
    loaded = sc.read(d)
    assert loaded.mb_release_id is None  # wrong tag cleared
    cand = loaded.mb_match_candidate
    assert cand.mb_release_id == "rel-correct"  # the suggestion
    # Mis-tag provenance is structured, NOT crammed into the matcher's notes —
    # both releases named, with disambiguation kept separate from the title.
    assert cand.mistag_tagged_mbid == "rel-wrong"
    assert cand.mistag_tagged_label == "Cell / Live in Corfu"
    assert not cand.mistag_tagged_disambig  # standard edition has none
    assert cand.mistag_owned_label == "Cell / Live in Corfu"
    assert cand.mistag_owned_disambig == "24-bit"
    assert cand.mistag_owned_url == "https://ultimae.bandcamp.com/album/live-in-corfu-24bit"
    assert cand.mistag_release_group_mbid == "rg-1"
    assert not any("release group" in n for n in cand.notes)
    album = next(a for a in scanner.scan(cfg.paths.music_dir) if a.path == d)
    assert album.state == AlbumState.NEEDS_MBID
    msgs = [e.message for e in activity.recent(10)]
    assert any("Possible mis-tag" in m and "Cell / Live in Corfu" in m for m in msgs)
    # #97: the warning names and links its album. The id is read back AFTER the
    # demote rewrote the sidecar (which clears the MBID), so it still resolves.
    entry = next(e for e in activity.recent(10) if "Possible mis-tag" in e.message)
    assert entry.album_id == _id_for(cfg, d)
    # Named from the album ON DISK, not the MusicBrainz release — the feed should
    # call it what the user's library calls it.
    assert entry.album_label == album.title


def test_detect_mistag_no_action_when_no_owned_sibling(cfg):
    """No edition in the album's release group is owned → not a mis-tag."""
    from harmonist.web.main import _detect_mistags_after_sync

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    d = _needs_sync_album(cfg, "Solo", "rel-a")
    syncer = _FakeSyncer([(1, "https://x.bandcamp.com/album/unrelated", "X / Other")])
    _detect_mistags_after_sync(
        cfg,
        syncer,
        # The group's only other edition isn't among the owned purchases.
        browse_rg=lambda rg: [("rel-b", ["https://x.bandcamp.com/album/different"])],
        fetch_release=lambda m: _release_with_rg(m, "rg-a"),
    )
    assert sc.read(d).mb_release_id == "rel-a"  # untouched


def test_detect_mistag_skips_when_multiple_owned_editions(cfg):
    """You own two editions in the album's release group → ambiguous → skip."""
    from harmonist.web.main import _detect_mistags_after_sync

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    d = _needs_sync_album(cfg, "Amb", "rel-wrong")
    syncer = _FakeSyncer(
        [
            (1, "https://x.bandcamp.com/album/a", "X / A"),
            (2, "https://x.bandcamp.com/album/b", "X / B"),
        ]
    )
    _detect_mistags_after_sync(
        cfg,
        syncer,
        browse_rg=lambda rg: [
            ("rel-a", ["https://x.bandcamp.com/album/a"]),
            ("rel-b", ["https://x.bandcamp.com/album/b"]),  # both owned → ambiguous
        ],
        fetch_release=lambda m: _release_with_rg(m, "rg-1"),
    )
    assert sc.read(d).mb_release_id == "rel-wrong"  # ambiguous → untouched


def test_detect_mistag_bails_over_album_cap(cfg, monkeypatch):
    """If the unmatched-album set exceeds the cap, bail with an Activity warning
    (the cap is on Set A now, not on the owned-purchase count)."""
    from harmonist import activity
    from harmonist.web import main as main_mod

    monkeypatch.setattr(main_mod, "_MISTAG_DETECTION_MAX_ALBUMS", 1)
    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    _needs_sync_album(cfg, "A1", "rel-1")
    _needs_sync_album(cfg, "A2", "rel-2")  # 2 albums > cap of 1
    syncer = _FakeSyncer([(1, "https://x.bandcamp.com/album/owned", "X / Owned")])
    called: list[str] = []

    def browse_rg(rg: str) -> list[tuple[str, list[str]]]:
        called.append(rg)
        return []

    activity.clear()
    main_mod._detect_mistags_after_sync(
        cfg,
        syncer,
        browse_rg=browse_rg,
        fetch_release=lambda m: _release_with_rg(m, "rg"),
    )
    assert called == []  # bailed before any browse
    assert any(
        "Mis-tag detection skipped" in e.message and e.level == "warning"
        for e in activity.recent(10)
    )


def test_link_unmatched_by_release_urls_links_via_alternate_slug(cfg):
    """A release with two Bandcamp URLs (…/idleness and …/idleness-2): the album
    is tagged with one slug but the purchase used the other → the plain slug
    match misses it. Linking via the release's *full* URL set fills item_id."""
    from harmonist.web.main import _link_unmatched_by_release_urls

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    d = _needs_sync_album(cfg, "Idleness", "rel-x")  # store_url slug 'idleness'
    syncer = _FakeSyncer([(555, "https://yann.bandcamp.com/album/idleness-2", "Yann / Idleness")])
    _link_unmatched_by_release_urls(
        cfg,
        syncer,
        fetch_urls=lambda m: [
            "https://yann.bandcamp.com/album/idleness",
            "https://yann.bandcamp.com/album/idleness-2",  # the purchase's slug
        ],
    )
    linked = sc.read(d)
    assert linked.bandcamp.item_id == 555  # Needs Link → Library
    assert linked.mb_release_id == "rel-x"  # tag preserved
    assert linked.store_url == "https://yann.bandcamp.com/album/idleness-2"  # purchase URL adopted


def test_link_unmatched_by_release_urls_no_match_leaves_album(cfg):
    """No owned purchase among the release's URLs → album untouched (it goes on
    to mis-tag detection / surrender)."""
    from harmonist.web.main import _link_unmatched_by_release_urls

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    d = _needs_sync_album(cfg, "Solo", "rel-y")
    syncer = _FakeSyncer([(7, "https://x.bandcamp.com/album/unrelated", "X / Other")])
    _link_unmatched_by_release_urls(
        cfg,
        syncer,
        fetch_urls=lambda m: [
            "https://x.bandcamp.com/album/solo",
            "https://x.bandcamp.com/album/solo-2",
        ],
    )
    r = sc.read(d)  # still Needs Link: no item_id, store_url not adopted
    assert r.bandcamp is None or r.bandcamp.item_id is None
    assert r.store_url == "https://label.bandcamp.com/album/solo"


def test_reject_clears_candidate(client, cfg):
    d = _make_album(cfg, "RC")
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=1),
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-zzz",
                confidence="approximate",
                file_count=1,
                track_count=1,
            ),
        ),
    )
    aid = _id_for(cfg, d)
    r = client.post(f"/reject/{aid}")
    assert r.status_code == 200
    loaded = sc.read(d)
    assert loaded.mb_match_candidate is None


def test_unconfirmed_url_update(client, cfg):
    d = _make_album(cfg, "UB")
    audio = MP4(d / "01 Track.m4a")
    audio[ATOM_MB_ALBUM_ID] = [b"rel-aaa"]
    audio.save()
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/old",
            bandcamp=BandcampInfo(item_id=None),
            mb_release_id="rel-aaa",
            tagged_at=datetime.now(UTC),
        ),
    )
    aid = _id_for(cfg, d)
    r = client.post(f"/unconfirmed/{aid}/url", data={"url": "https://x.bandcamp.com/album/new"})
    assert r.status_code == 200
    loaded = sc.read(d)
    assert loaded.store_url == "https://x.bandcamp.com/album/new"
    assert loaded.bandcamp is None or loaded.bandcamp.item_id is None


def test_unconfirmed_mark_manual(client, cfg):
    d = _make_album(cfg, "UB2")
    audio = MP4(d / "01 Track.m4a")
    audio[ATOM_MB_ALBUM_ID] = [b"rel-aaa"]
    audio.save()
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=None),
            mb_release_id="rel-aaa",
            tagged_at=datetime.now(UTC),
        ),
    )
    aid = _id_for(cfg, d)
    r = client.post(f"/unconfirmed/{aid}/manual")
    assert r.status_code == 200
    loaded = sc.read(d)
    assert loaded.store_url is None
    assert loaded.bandcamp is None
    assert loaded.mb_release_id == "rel-aaa"  # preserved


def test_404_for_missing_album(client):
    r = client.post("/recheck/nonexistent")
    assert r.status_code == 404


# ---------- manual ingest flow ----------


def test_manual_search_returns_results(client, cfg, monkeypatch):
    d = _make_album(cfg, "Search Album")
    aid = _id_for(cfg, d)
    monkeypatch.setattr(
        "harmonist.mb_search.search_releases",
        lambda artist, title, limit=10: [
            {
                "id": "rel-1",
                "title": "Result A",
                "artist": "X",
                "date": "2020",
                "country": "GB",
                "status": "Official",
                "track_count": 10,
                "label": "Label",
                "catalog_number": "CAT1",
            },
            {
                "id": "rel-2",
                "title": "Result B",
                "artist": "X",
                "date": "2021",
                "country": "US",
                "status": "Official",
                "track_count": 12,
                "label": None,
                "catalog_number": None,
            },
        ],
    )
    r = client.post(f"/manual/{aid}/search", data={"artist": "X", "title": "Y"})
    assert r.status_code == 200
    assert "Result A" in r.text
    assert "Result B" in r.text
    assert "rel-1" in r.text
    assert 'name="mbid"' in r.text  # hidden mbid input on each "Use" button
    # Each row links out to the release on MusicBrainz for closer inspection.
    assert "musicbrainz.org/release/rel-1" in r.text
    assert "musicbrainz.org/release/rel-2" in r.text
    # The local album has 1 file; both results (10/12 tracks) mismatch and are
    # struck through with an explanatory tooltip.
    assert "line-through" in r.text
    assert "your album has 1 file" in r.text


def test_manual_search_no_strikethrough_on_track_count_match(client, cfg, monkeypatch):
    """A candidate whose track count matches the local file count isn't flagged."""
    d = _make_album(cfg, "MatchCount")  # 1 file
    aid = _id_for(cfg, d)
    monkeypatch.setattr(
        "harmonist.mb_search.search_releases",
        lambda artist, title, limit=10: [
            {"id": "rel-9", "title": "Exactly One", "artist": "X", "track_count": 1}
        ],
    )
    r = client.post(f"/manual/{aid}/search", data={"artist": "X", "title": "Y"})
    assert "rel-9" in r.text
    assert "line-through" not in r.text


def test_manual_search_caps_results_at_five(client, cfg, monkeypatch):
    """The route requests at most 5 from MB — beyond that, MB's own search."""
    seen = {}

    def fake_search(artist, title, limit=10):
        seen["limit"] = limit
        return []

    monkeypatch.setattr("harmonist.mb_search.search_releases", fake_search)
    d = _make_album(cfg, "CapSearch")
    aid = _id_for(cfg, d)
    client.post(f"/manual/{aid}/search", data={"artist": "X", "title": "Y"})
    assert seen["limit"] == 5


def test_manual_search_empty_results(client, cfg, monkeypatch):
    d = _make_album(cfg, "NoResults")
    aid = _id_for(cfg, d)
    monkeypatch.setattr(
        "harmonist.mb_search.search_releases",
        lambda artist, title, limit=10: [],
    )
    r = client.post(f"/manual/{aid}/search", data={"artist": "X", "title": "Y"})
    assert r.status_code == 200
    assert "No matches" in r.text


def test_manual_search_handles_mb_error(client, cfg, monkeypatch):
    d = _make_album(cfg, "Errsearch")
    aid = _id_for(cfg, d)
    from harmonist.mb_search import MBSearchError

    def explode(artist, title, limit=10):
        raise MBSearchError("MB down")

    monkeypatch.setattr("harmonist.mb_search.search_releases", explode)
    r = client.post(f"/manual/{aid}/search", data={"artist": "X", "title": "Y"})
    assert r.status_code == 200
    assert "MB search failed" in r.text


def _needs_mbid_with_store_url(cfg, name: str, store_url: str):
    """A NEEDS_MBID album (store URL on file, no MBID yet) for picker tests."""
    d = _make_album(cfg, name)
    sc.write(
        d,
        Sidecar(store_url=store_url),
    )
    return d


def test_needs_mbid_store_url_offers_both_search_modes(client, cfg):
    """A Needs-MBID card with a store URL renders ONE find-a-release area with
    a radio to pick the search method (store URL vs name) — both controls
    present, mutually exclusive via the .find-mode :has() rule."""
    d = _needs_mbid_with_store_url(cfg, "Modes", "https://x.bandcamp.com/album/y")
    aid = _id_for(cfg, d)
    r = client.get("/tasks")
    assert "Find a different release" in r.text
    # Both radio options + their controls render (one results box for both).
    assert 'value="url"' in r.text
    assert 'value="name"' in r.text
    assert "Look up releases at this URL" in r.text  # store-URL mode action
    assert f"/manual/{aid}/search" in r.text  # name-mode form
    assert r.text.count('id="mbid-results-') == 1  # single shared results box
    # No nested "search by name" disclosure anymore.
    assert "Or search MusicBrainz by name" not in r.text


def test_manual_search_results_carry_heading(client, cfg, monkeypatch):
    """Name-search results are headed so it's clear which search ran."""
    d = _make_album(cfg, "Headed")
    aid = _id_for(cfg, d)
    monkeypatch.setattr(
        "harmonist.mb_search.search_releases",
        lambda artist, title, limit=10: [
            {"id": "rel-h", "title": "H", "artist": "X", "track_count": 1}
        ],
    )
    r = client.post(f"/manual/{aid}/search", data={"artist": "X", "title": "Y"})
    assert "MusicBrainz search results" in r.text


def test_manual_candidates_lists_store_url_releases(client, cfg, monkeypatch):
    """The Choose-release picker lists the MB releases linked to the store URL,
    with disambiguation + media, each with a Use button and MB link."""
    cross = "\N{MULTIPLICATION SIGN}"
    d = _needs_mbid_with_store_url(cfg, "PickMe", "https://x.bandcamp.com/album/y")
    aid = _id_for(cfg, d)
    monkeypatch.setattr(
        "harmonist.mb_lookup.candidate_summaries_for_url",
        lambda url: (
            [
                {
                    "id": "rel-std",
                    "title": "Life²",
                    "disambiguation": "",
                    "artist": "Asura",
                    "track_count": 11,
                    "media": "CD",
                    "date": "2010",
                    "country": "FR",
                    "status": "Official",
                    "label": "Ultimae",
                    "catalog_number": "ULT-1",
                },
                {
                    "id": "rel-24",
                    "title": "Life²",
                    "disambiguation": "24-bit",
                    "artist": "Asura",
                    "track_count": 11,
                    "media": f"2{cross}CD",
                    "date": "2010",
                    "country": "FR",
                    "status": "Official",
                    "label": "Ultimae",
                    "catalog_number": "ULT-2",
                },
            ],
            2,
        ),
    )
    r = client.post(f"/manual/{aid}/candidates")
    assert r.status_code == 200
    assert "Releases linked to this store URL" in r.text
    assert "musicbrainz.org/release/rel-std" in r.text
    assert "musicbrainz.org/release/rel-24" in r.text
    assert "(24-bit)" in r.text  # disambiguation rendered distinctly
    assert f"2{cross}CD" in r.text  # media summary
    assert r.text.count('name="mbid"') == 2  # a Use button per release


def test_recheck_escapes_mb_error_text_in_the_flash(client, cfg, monkeypatch):
    """#142: an MBError message reaches the flash fragment as text, not markup.

    The message wraps upstream musicbrainzngs output, so it originates off-box.
    The diagnostic still has to be readable — this pins escaped, not dropped.
    """
    d = _needs_mbid_with_store_url(cfg, "Hostile", "https://x.bandcamp.com/album/y")
    aid = _id_for(cfg, d)

    def boom(url):
        raise mb_lookup.MBError('MB request failed: <script>alert(1)</script> & "quoted"')

    monkeypatch.setattr("harmonist.mb_lookup.lookup_by_bandcamp_url", boom)

    r = client.post(f"/recheck/{aid}")
    assert r.status_code == 200
    assert "<script>" not in r.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text
    assert "&amp;" in r.text and "&quot;quoted&quot;" in r.text
    assert "MB lookup failed" in r.text  # the diagnostic survives, escaped


def test_recheck_multiple_matches_shows_picker_not_autopick(client, cfg, monkeypatch):
    """When a store URL maps to several MB releases, Recheck stops guessing —
    it retargets the picker into the card's results box and leaves the album
    untagged for the user to choose."""
    d = _needs_mbid_with_store_url(cfg, "Ambiguous", "https://x.bandcamp.com/album/y")
    aid = _id_for(cfg, d)
    monkeypatch.setattr(
        "harmonist.mb_lookup.lookup_by_bandcamp_url",
        lambda url: ["rel-a", "rel-b"],
    )
    monkeypatch.setattr(
        "harmonist.mb_lookup.candidate_summaries_for_url",
        lambda url: (
            [
                {
                    "id": "rel-a",
                    "title": "A",
                    "disambiguation": "",
                    "artist": "X",
                    "track_count": 9,
                    "media": "CD",
                    "date": None,
                    "country": None,
                    "status": None,
                    "label": None,
                    "catalog_number": None,
                },
                {
                    "id": "rel-b",
                    "title": "B",
                    "disambiguation": "",
                    "artist": "X",
                    "track_count": 10,
                    "media": "Digital Media",
                    "date": None,
                    "country": None,
                    "status": None,
                    "label": None,
                    "catalog_number": None,
                },
            ],
            2,
        ),
    )
    r = client.post(f"/recheck/{aid}")
    assert r.status_code == 200
    # The button posts with hx-swap=none, so the response retargets the picker.
    assert r.headers.get("HX-Retarget") == f"#mbid-results-{aid}"
    assert r.headers.get("HX-Reswap") == "innerHTML"
    assert "pick the right one" in r.text
    assert "musicbrainz.org/release/rel-a" in r.text
    # No silent auto-pick: the album stays untagged.
    loaded = sc.read(d)
    assert loaded.mb_release_id is None
    assert loaded.mb_match_candidate is None


def test_manual_assign_with_full_url(client, cfg, monkeypatch):
    """Pasting an MB release URL should extract the MBID."""
    d = _make_album(cfg, "AssignURL")
    aid = _id_for(cfg, d)
    captured = {}

    def fake_fetch(mbid):
        captured["mbid"] = mbid
        return _release_for_match(mbid, n_tracks=1)

    monkeypatch.setattr("harmonist.mb_lookup.fetch_release", fake_fetch)
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)

    r = client.post(
        f"/manual/{aid}/assign",
        data={"mbid": "https://musicbrainz.org/release/abc12345-1234-1234-1234-1234567890ab"},
    )
    assert r.status_code == 200
    assert captured["mbid"] == "abc12345-1234-1234-1234-1234567890ab"
    loaded = sc.read(d)
    assert loaded.store_url is None
    assert loaded.mb_release_id == "abc12345-1234-1234-1234-1234567890ab"


def test_manual_assign_with_bare_mbid(client, cfg, monkeypatch):
    d = _make_album(cfg, "AssignMBID")
    aid = _id_for(cfg, d)

    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release",
        lambda mbid: _release_for_match(mbid, n_tracks=1),
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)

    r = client.post(
        f"/manual/{aid}/assign",
        data={"mbid": "abc12345-1234-1234-1234-1234567890ab"},
    )
    assert r.status_code == 200
    loaded = sc.read(d)
    assert loaded.mb_release_id == "abc12345-1234-1234-1234-1234567890ab"


_ASSIGN_MBID = "abc12345-1234-1234-1234-1234567890ab"


def _state_of(cfg, album_dir):
    from harmonist import scanner

    for a in scanner.scan(cfg.paths.music_dir):
        if a.path == album_dir:
            return a.state
    raise AssertionError(f"no album at {album_dir}")


def test_manual_assign_derives_store_url_from_embedded_comment(client, cfg, monkeypatch):
    """A manual download with a precise /album/ URL in ©cmt → store_url recorded
    from the comment (no MB url-rel lookup) → album lands in Needs Link."""
    from harmonist.models import AlbumState

    d = _make_album(cfg, "EmbeddedURL", comment="https://artist.bandcamp.com/album/x")
    aid = _id_for(cfg, d)
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release", lambda m: _release_for_match(m, n_tracks=1)
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)

    def no_urls(_m):
        raise AssertionError("MB url-rels must not be queried when ©cmt has a precise URL")

    monkeypatch.setattr("harmonist.mb_lookup.fetch_release_urls", no_urls)

    r = client.post(f"/manual/{aid}/assign", data={"mbid": _ASSIGN_MBID})
    assert r.status_code == 200
    assert sc.read(d).store_url == "https://artist.bandcamp.com/album/x"
    assert _state_of(cfg, d) == AlbumState.NEEDS_SYNC


def test_manual_assign_falls_back_to_mb_url_when_comment_is_root(client, cfg, monkeypatch):
    """Artist-root ©cmt + MB has a Bandcamp url-rel → MB's canonical URL recorded."""
    from harmonist.models import AlbumState

    d = _make_album(cfg, "RootURL", comment="Visit https://artist.bandcamp.com")
    aid = _id_for(cfg, d)
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release", lambda m: _release_for_match(m, n_tracks=1)
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release_urls",
        lambda _m: ["https://artist.bandcamp.com/album/canonical"],
    )

    r = client.post(f"/manual/{aid}/assign", data={"mbid": _ASSIGN_MBID})
    assert r.status_code == 200
    assert sc.read(d).store_url == "https://artist.bandcamp.com/album/canonical"
    assert _state_of(cfg, d) == AlbumState.NEEDS_SYNC


def test_manual_assign_uses_artist_root_placeholder_when_no_mb_url(client, cfg, monkeypatch):
    """Artist-root ©cmt + MB has no Bandcamp url-rel → keep the artist-root as a
    placeholder store_url so it's still recognised as a Bandcamp purchase."""
    from harmonist.models import AlbumState

    d = _make_album(cfg, "Placeholder", comment="Visit https://artist.bandcamp.com")
    aid = _id_for(cfg, d)
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release", lambda m: _release_for_match(m, n_tracks=1)
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)
    monkeypatch.setattr("harmonist.mb_lookup.fetch_release_urls", lambda _m: [])

    r = client.post(f"/manual/{aid}/assign", data={"mbid": _ASSIGN_MBID})
    assert r.status_code == 200
    assert sc.read(d).store_url == "https://artist.bandcamp.com"
    assert _state_of(cfg, d) == AlbumState.NEEDS_SYNC


def test_manual_assign_store_url_derivation_error_is_swallowed(client, cfg, monkeypatch):
    """If store_url derivation raises, tagging still succeeds (store_url stays None)."""
    d = _make_album(cfg, "BoomURL")
    aid = _id_for(cfg, d)
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release", lambda m: _release_for_match(m, n_tracks=1)
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)

    def boom(*a, **kw):
        raise RuntimeError("derivation failed")

    monkeypatch.setattr("harmonist.reconcile.store_url_for_tagging", boom)

    r = client.post(f"/manual/{aid}/assign", data={"mbid": _ASSIGN_MBID})
    assert r.status_code == 200
    loaded = sc.read(d)
    assert loaded.mb_release_id == _ASSIGN_MBID  # tagged despite the derivation error
    assert loaded.store_url is None


def test_manual_assign_invalid_input(client, cfg):
    """Input that's neither an MBID-shaped token nor an MB URL is rejected."""
    d = _make_album(cfg, "Bad")
    aid = _id_for(cfg, d)
    # Whitespace + punctuation that can't be an MBID
    r = client.post(f"/manual/{aid}/assign", data={"mbid": "this has spaces!"})
    assert r.status_code == 200
    assert "Could not parse" in r.text
    assert not sc.has_sidecar(d)


def test_manual_assign_with_approximate_match_stores_candidate(client, cfg, monkeypatch):
    d = _make_album(cfg, "Approximate")
    aid = _id_for(cfg, d)

    # Release with one track, but a length way off → "approximate"
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release",
        lambda mbid: _release_for_match(mbid, n_tracks=1, length_ms=99999),
    )

    r = client.post(
        f"/manual/{aid}/assign",
        data={"mbid": "abc12345-1234-1234-1234-1234567890ab"},
    )
    assert r.status_code == 200
    assert "review and confirm" in r.text
    loaded = sc.read(d)
    assert loaded.mb_release_id is None
    assert loaded.mb_match_candidate is not None
    assert loaded.mb_match_candidate.confidence == "approximate"


# ---------- URL recovery ----------


def _release_for_match(mbid: str, *, n_tracks: int, length_ms: int = 1000) -> dict:
    return {
        "id": mbid,
        "title": "Title",
        "release-group": {"id": "rg-1", "primary-type": "Album"},
        "artist-credit": [{"artist": {"id": "art-1", "name": "Artist"}, "name": "Artist"}],
        "medium-list": [
            {
                "position": "1",
                "track-list": [
                    {
                        "id": f"rt-{i}",
                        "position": str(i),
                        "title": f"Track {i}",
                        "recording": {
                            "id": f"rec-{i}",
                            "title": f"Track {i}",
                            "length": str(length_ms),
                        },
                    }
                    for i in range(1, n_tracks + 1)
                ],
            }
        ],
    }


# ---------- cover route ----------


def test_cover_returns_404_when_absent(client, cfg):
    d = _make_album(cfg, "NoCover")
    aid = _id_for(cfg, d)
    r = client.get(f"/cover/{aid}")
    assert r.status_code == 404


# ---------- library ----------


def _make_tagged_album(cfg, name: str, *, mbid: str, tagged_at, item_id: int | None = None) -> Path:
    """Create a Done-state album with sidecar + matching MBID tag on the file."""
    d = _make_album(cfg, name)
    audio = MP4(d / "01 Track.m4a")
    audio[ATOM_MB_ALBUM_ID] = [mbid.encode("utf-8")]
    audio.save()
    from harmonist.models import BandcampInfo, Sidecar

    sc.write(
        d,
        Sidecar(
            store_url=(
                f"https://x.bandcamp.com/album/{name.lower().replace(' ', '-')}"
                if item_id
                else None
            ),
            bandcamp=BandcampInfo(item_id=item_id) if item_id else None,
            mb_release_id=mbid,
            tagged_at=tagged_at,
        ),
    )
    return d


def test_library_renders_only_done_albums(client, cfg):
    from datetime import datetime

    _make_album(cfg, "NewAlbum")
    _make_tagged_album(cfg, "DoneOne", mbid="rel-1", tagged_at=datetime.now(UTC), item_id=100)
    r = client.get("/library")
    assert r.status_code == 200
    assert "DoneOne" in r.text
    assert "NewAlbum" not in r.text


def test_library_sorted_by_tagged_at_desc(client, cfg):
    from datetime import datetime, timedelta

    base = datetime.now(UTC)
    _make_tagged_album(cfg, "Old", mbid="rel-old", tagged_at=base - timedelta(days=5), item_id=1)
    _make_tagged_album(cfg, "Mid", mbid="rel-mid", tagged_at=base - timedelta(days=2), item_id=2)
    _make_tagged_album(cfg, "Recent", mbid="rel-recent", tagged_at=base, item_id=3)
    r = client.get("/library")
    text = r.text
    # Recent appears before Mid which appears before Old
    assert text.index("Recent") < text.index("Mid") < text.index("Old")


def _make_library(cfg, count):
    """`count` tagged albums, newest first: Album0 is the most recently tagged,
    so page 1 of the grid holds Album0…Album{limit-1} in order."""
    from datetime import datetime, timedelta

    base = datetime.now(UTC)
    for i in range(count):
        _make_tagged_album(
            cfg, f"Album{i}", mbid=f"rel-{i}", tagged_at=base - timedelta(days=i), item_id=i + 1
        )


def test_library_first_page_holds_one_limit_of_rows(client, cfg):
    _make_library(cfg, 5)
    r = client.get("/library?page=1&limit=2")
    assert r.status_code == 200
    assert r.text.count('id="lib-') == 2
    assert "Album0" in r.text and "Album1" in r.text
    assert "Album2" not in r.text
    # The tab-badge total reflects ALL done albums (5), not the 2 rendered —
    # this is the attribute the Library tab count reads.
    assert 'data-total-done="5"' in r.text


def test_library_page_two_holds_the_next_slice(client, cfg):
    _make_library(cfg, 5)
    r = client.get("/library?page=2&limit=2")
    assert r.text.count('id="lib-') == 2
    assert "Album2" in r.text and "Album3" in r.text
    assert "Album0" not in r.text
    # Every page is a whole render now, header included — it replaces the grid
    # rather than appending to it, so page 2 must stand on its own.
    assert "<h2" in r.text
    assert 'data-page="2"' in r.text


def test_library_pager_links_carry_prev_and_next(client, cfg):
    _make_library(cfg, 5)
    body = client.get("/library?page=2&limit=2").text
    assert 'hx-get="/library?page=1&limit=2"' in body
    assert 'hx-get="/library?page=3&limit=2"' in body
    # The pushed URL and the href agree, so the address bar names what's on
    # screen and a middle-click opens the same page. Both carry the page SIZE as
    # well as the number (#144) — without it the same link resolves to different
    # albums once the reader picks a different size.
    assert 'href="/?tab=library&page=3&limit=2"' in body
    assert 'hx-push-url="/?tab=library&page=3&limit=2"' in body
    assert "3–4 of 5" in body  # row range readout
    assert "page 2 of 3" in body


def test_library_pager_has_no_link_past_either_end(client, cfg):
    _make_library(cfg, 5)
    first = client.get("/library?page=1&limit=2").text
    assert 'hx-get="/library?page=0' not in first
    assert 'aria-disabled="true"' in first  # Previous is inert, not a link

    last = client.get("/library?page=3&limit=2").text
    assert 'hx-get="/library?page=4' not in last
    assert 'aria-disabled="true"' in last


def test_library_pager_absent_when_everything_fits_on_one_page(client, cfg):
    _make_library(cfg, 2)
    body = client.get("/library?page=1&limit=30").text
    assert "Previous" not in body
    assert "Next" not in body


def test_library_page_beyond_the_end_clamps_to_the_last_page(client, cfg):
    """A page number outlives the albums that filled it — a bookmark, or Back
    after a sync removed some. Clamping keeps a saved link pointing at albums
    instead of resolving to a blank grid that reads as "my library is gone"."""
    _make_library(cfg, 5)
    r = client.get("/library?page=99&limit=2")
    assert r.status_code == 200
    assert 'data-page="3"' in r.text
    assert "Album4" in r.text


def test_library_page_below_one_clamps_up(client, cfg):
    _make_library(cfg, 5)
    r = client.get("/library?page=-3&limit=2")
    assert 'data-page="1"' in r.text
    assert "Album0" in r.text


def test_library_page_size_control_offers_the_sizes_and_marks_the_current_one(client, cfg):
    _make_library(cfg, 3)
    body = client.get("/library?limit=40").text
    for size in (20, 40, 60):
        assert f'<option value="{size}"' in body
    assert '<option value="40" selected>' in body
    assert '<option value="20" selected>' not in body
    # A real form, so it still works with JS off; and the size control must not
    # carry an onclick beside its hx-* (template-lint's rule, #40).
    assert 'method="get" action="/"' in body
    assert "onclick" not in body
    # `submit` must stay in the trigger list beside `change`. Naming `change`
    # alone REPLACES a form's default `submit` trigger, and every submit event
    # then escapes HTMX into a full page navigation. The browser half of this is
    # in test/e2e/test_library_page_size.py, which most runs skip — this is the
    # cheap guard against the attribute being tidied by someone who never sees it.
    assert 'hx-trigger="change, submit"' in body


def test_library_page_size_control_is_absent_when_there_is_nothing_to_size(client, cfg):
    """An empty Library gets the empty state, not an offer to show 40 of them."""
    body = client.get("/library").text
    assert "No fully-tagged albums yet." in body
    assert 'id="library-limit"' not in body


def test_library_page_size_control_shows_an_off_menu_size_rather_than_lying(client, cfg):
    """A hand-typed `?limit=7` is honoured, so the control has to be able to say
    so. Snapping the <select> to a neighbouring option would make it report a page
    size that isn't the one on screen."""
    _make_library(cfg, 3)
    body = client.get("/library?limit=7").text
    assert "<option selected>7</option>" in body
    assert '<option value="20" selected>' not in body


def test_library_default_page_size_is_twenty(client, cfg):
    _make_library(cfg, 25)
    r = client.get("/library")
    assert r.text.count('id="lib-') == 20
    assert "1–20 of 25" in r.text


def test_library_remembers_a_chosen_page_size_for_the_next_bare_visit(client, cfg):
    """The size the reader picked has to survive the trip to an album page and
    back, and the URL there carries only `?page=`. The cookie is what makes a
    later bare request render their size in the FIRST paint, rather than painting
    the default and correcting it (#144)."""
    _make_library(cfg, 25)
    chosen = client.get("/library?limit=40")
    assert chosen.cookies["harmonist-library-limit"] == "40"

    # TestClient carries the cookie forward, exactly as a browser would.
    assert client.get("/library").text.count('id="lib-') == 25


def test_library_does_not_remember_a_size_the_reader_never_chose(client, cfg):
    """Only an explicit `?limit=` is a choice. Writing the cookie on every render
    would pin readers to a default they never picked."""
    _make_library(cfg, 3)
    assert "harmonist-library-limit" not in client.get("/library").cookies


def test_library_survives_an_unreadable_remembered_size(client, cfg):
    """The cookie outlives the code that wrote it and anyone can edit it. A bad
    value must degrade to the default page, never to a 500."""
    _make_library(cfg, 25)
    client.cookies.set("harmonist-library-limit", "not-a-number")
    r = client.get("/library")
    assert r.status_code == 200
    assert r.text.count('id="lib-') == 20


def test_library_page_size_change_keeps_the_album_you_were_looking_at_on_screen(client, cfg):
    """Carrying the page NUMBER across a size change teleports the reader: page 3
    is rows 41–60 at 20 per page and rows 81–120 at 40. The anchor resolves against
    the new size instead, so the row at the top of the screen stays on screen."""
    _make_library(cfg, 100)
    # Reader is on page 3 of 20 — rows 41–60, i.e. Album40 at the top.
    assert "41–60 of 100" in client.get("/library?page=3&limit=20").text
    # Switching to 40 per page must land on the page holding row 41, not page 3.
    r = client.get("/library?limit=40&anchor=41")
    assert 'data-page="2"' in r.text
    assert "41–80 of 100" in r.text
    assert "Album40" in r.text
    # Only the server knows where the anchor landed, so it corrects the address
    # bar itself rather than leaving a guessed hx-push-url on the control.
    assert r.headers["HX-Push-Url"] == "/?tab=library&page=2&limit=40"


def test_library_refresh_does_not_push_a_history_entry(client, cfg):
    """The grid re-requests itself on every `tasks-changed`. Pushing a URL per
    background refresh would bury the Back button under identical entries — so the
    header is confined to the anchor case."""
    _make_library(cfg, 25)
    assert "HX-Push-Url" not in client.get("/library?page=2&limit=20").headers


def test_library_page_size_rides_in_the_index_url(client, cfg):
    """`?limit=` belongs to the same addressable view as `?tab=` and `?page=`, so
    a shared link resolves to exactly the albums the sender was looking at."""
    _make_library(cfg, 100)
    body = client.get("/?tab=library&page=2&limit=40").text
    assert body.count('id="lib-') == 40
    assert "41–80 of 100" in body


def test_library_page_size_is_bounded(client, cfg):
    """An unbounded `?limit=` would put a whole library through the template on
    one request."""
    _make_library(cfg, 3)
    r = client.get("/library?limit=100000")
    assert r.status_code == 200
    assert 'data-limit="200"' in r.text


def test_library_empty_still_renders_its_header(client, cfg):
    r = client.get("/library")
    assert "No fully-tagged albums yet." in r.text
    assert 'data-total-done="0"' in r.text


def test_library_empty_state(client):
    r = client.get("/library")
    assert "No fully-tagged albums yet" in r.text


def test_library_tile_is_a_link_to_the_album_page(client, cfg):
    """#129: a tile is an ordinary link, not a dialog trigger — so every click
    has a real URL that can be shared, bookmarked and gone back from."""
    from datetime import datetime

    d = _make_tagged_album(cfg, "Linked", mbid="abc-123", tagged_at=datetime.now(UTC), item_id=42)
    r = client.get("/library")
    assert f'href="/album/{_id_for(cfg, d)}"' in r.text
    # Guards the route's re-introduction: a tile must never queue a fragment
    # fetch to open in place, which is what #129 removed.
    assert "/detail" not in r.text
    # The heavy detail markup is still not inlined in the grid response.
    assert "musicbrainz.org/release/abc-123" not in r.text


def test_library_detail_links_to_musicbrainz(client, cfg):
    from datetime import datetime

    d = _make_tagged_album(cfg, "Linked", mbid="abc-123", tagged_at=datetime.now(UTC), item_id=42)
    r = client.get(f"/album/{_id_for(cfg, d)}")
    assert "musicbrainz.org/release/abc-123" in r.text


def test_library_detail_shows_store_url_and_item_id(client, cfg):
    from datetime import datetime

    d = _make_tagged_album(cfg, "Linked", mbid="abc-123", tagged_at=datetime.now(UTC), item_id=42)
    r = client.get(f"/album/{_id_for(cfg, d)}")
    assert "x.bandcamp.com" in r.text
    assert "42" in r.text  # item_id


def test_library_detail_omits_bandcamp_removal_controls(client, cfg):
    """#42 interim: the Bandcamp link-removal controls (× forget-URL and Unlink) are
    temporarily pulled — with no way to re-link a Library album, removing a link is a
    dead end. Even a linked album shows no removal control. (The /unlink endpoint
    still exists; only the UI is removed.)"""
    linked = _make_tagged_album(
        cfg, "IsLinked", mbid="rel-l", tagged_at=datetime.now(UTC), item_id=7
    )
    html = client.get(f"/album/{_id_for(cfg, linked)}").text
    assert "/unlink" not in html
    assert "forget_url" not in html


def test_unlink_reverts_complete_album_to_needs_sync(client, cfg):
    """Unlinking clears the purchase item_id (keeps tags + store_url), so the album
    reverts from Library/COMPLETE to NEEDS_SYNC and can be re-linked."""
    from datetime import datetime

    from harmonist import activity
    from harmonist.models import AlbumState
    from harmonist.scanner import scan

    d = _make_tagged_album(cfg, "Undo Me", mbid="rel-u", tagged_at=datetime.now(UTC), item_id=99)
    assert next(a for a in scan(cfg.paths.music_dir) if a.path == d).state == AlbumState.COMPLETE

    activity.clear()
    r = client.post(f"/library/{_id_for(cfg, d)}/unlink")
    assert r.status_code == 200
    loaded = sc.read(d)
    # Link cleared (an all-None bandcamp block is dropped on write).
    assert loaded.bandcamp is None or loaded.bandcamp.item_id is None
    assert loaded.store_url == "https://x.bandcamp.com/album/undo-me"  # kept
    assert next(a for a in scan(cfg.paths.music_dir) if a.path == d).state == AlbumState.NEEDS_SYNC
    assert any("Unlinked" in e.message and "99" in e.message for e in activity.recent(5))


def test_unlink_wrong_match_forgets_url_and_stays_in_library(client, cfg):
    """'Wrong match' unlink also drops the store_url, so the album stays a
    correctly-tagged Library album (COMPLETE) and no sync can re-link it — unlike a
    plain unlink, which keeps the (wrong) URL and would re-link on the next sync."""
    from datetime import datetime

    from harmonist.bandcamp_hook import survey_album_links
    from harmonist.models import AlbumState
    from harmonist.scanner import scan

    d = _make_tagged_album(cfg, "Wrong Link", mbid="rel-w", tagged_at=datetime.now(UTC), item_id=42)
    assert next(a for a in scan(cfg.paths.music_dir) if a.path == d).state == AlbumState.COMPLETE

    r = client.post(f"/library/{_id_for(cfg, d)}/unlink", data={"forget_url": "true"})
    assert r.status_code == 200
    loaded = sc.read(d)
    assert loaded.store_url is None  # URL forgotten
    assert loaded.bandcamp is None
    assert loaded.mb_release_id == "rel-w"  # still correctly tagged
    # Stays in the Library (not Needs Link), and no sync will re-link it: with no
    # store_url it's invisible to both the slug matcher and adoption.
    assert next(a for a in scan(cfg.paths.music_dir) if a.path == d).state == AlbumState.COMPLETE
    by_slug, slugless, _ = survey_album_links(cfg.paths.music_dir)
    assert d not in slugless
    assert all(d not in paths for paths in by_slug.values())


def test_rematch_clears_mbid_and_demotes_to_needs_mbid(client, cfg):
    """Wrong MusicBrainz match → /rematch clears mb_release_id so the album
    derives back to Needs MBID, keeping the Bandcamp link and the on-disk tags
    intact for the user to re-pick + re-tag (#38)."""
    from harmonist.models import AlbumState
    from harmonist.scanner import scan

    d = _make_tagged_album(
        cfg, "Wrong MB", mbid="rel-wrong", tagged_at=datetime.now(UTC), item_id=7
    )
    assert next(a for a in scan(cfg.paths.music_dir) if a.path == d).state == AlbumState.COMPLETE
    # File carries the (wrong) MB Album Id before re-match.
    assert MP4(d / "01 Track.m4a")[ATOM_MB_ALBUM_ID][0].decode() == "rel-wrong"

    r = client.post(f"/library/{_id_for(cfg, d)}/rematch")
    assert r.status_code == 200
    assert "Needs MBID" in r.text

    loaded = sc.read(d)
    assert loaded.mb_release_id is None  # MB match cleared
    assert loaded.temp_uid is not None  # write() minted a local id (identity invariant)
    assert loaded.store_url is not None  # Bandcamp link kept — only MB was wrong
    assert loaded.bandcamp.item_id == 7
    # Non-destructive: the on-disk tag is untouched until the user re-tags.
    assert MP4(d / "01 Track.m4a")[ATOM_MB_ALBUM_ID][0].decode() == "rel-wrong"
    assert next(a for a in scan(cfg.paths.music_dir) if a.path == d).state == AlbumState.NEEDS_MBID


def test_rematch_warns_when_no_mb_release(client, cfg):
    """/rematch on an album with no MB release is a no-op warning, not an error."""
    d = _make_album(cfg, "NoMB")  # NEW, no sidecar mb_release_id
    r = client.post(f"/library/{_id_for(cfg, d)}/rematch")
    assert r.status_code == 200
    assert "Nothing to re-match" in r.text


def test_library_detail_wrong_match_controls_beside_badges(client, cfg):
    """The MB 'wrong match' re-pick (pencil → /rematch) sits beside the MusicBrainz
    badge (#38), replacing the old ambiguous 'Wrong match' text button. The Bandcamp
    forget-link control is temporarily removed pending a re-link path (#42)."""
    d = _make_tagged_album(cfg, "Badges", mbid="rel-b", tagged_at=datetime.now(UTC), item_id=9)
    aid = _id_for(cfg, d)
    html = client.get(f"/album/{aid}").text
    assert f"/library/{aid}/rematch" in html  # MB re-pick control exists
    assert "Wrong MusicBrainz match" in html  # explanatory tooltip
    assert "Wrong match" not in html  # old ambiguous button is gone
    assert "forget_url" not in html  # BC removal control pulled for now (#42)


def test_album_page_actions_navigate_after_success_not_onclick(client, cfg):
    """Regression for #40, now on the page instead of the dialog (#129): an
    action button whose side effect is to leave the page must run that side
    effect from hx-on::after-request — after htmx has done its confirm and its
    request — never from onclick. An onclick that navigates detaches the button
    before htmx's delegated handler runs, so the confirm never shows and the
    request never fires; the control silently does nothing but move you."""
    import re

    d = _make_tagged_album(cfg, "PageActs", mbid="rel-ma", tagged_at=datetime.now(UTC), item_id=5)
    html = client.get(f"/album/{_id_for(cfg, d)}").text
    assert "/rematch" in html  # the pencil sends the album out of the Library
    # No hx-post *action* button may use onclick to navigate.
    action_buttons = re.findall(r"<button[^>]*\shx-post[^>]*>", html)
    assert action_buttons
    assert all("onclick=" not in b for b in action_buttons)
    # The ones that navigate after acting use hx-on::after-request instead.
    assert any("hx-on::after-request" in b for b in action_buttons)


# ---------- retag / forget ----------


def test_retag_re_runs_tagger(client, cfg, monkeypatch):
    from datetime import datetime

    d = _make_tagged_album(
        cfg, "ToRetag", mbid="rel-1", tagged_at=datetime(2026, 1, 1, tzinfo=UTC), item_id=1
    )
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release",
        lambda mbid: _release_for_match(mbid, n_tracks=1),
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)

    aid = _id_for(cfg, d)
    r = client.post(f"/retag/{aid}")
    assert r.status_code == 200
    assert "Re-tagged" in r.text
    # tagged_at should be refreshed
    loaded = sc.read(d)
    assert loaded is not None
    assert loaded.tagged_at is not None
    assert loaded.tagged_at > datetime(2026, 1, 1, tzinfo=UTC)


def test_retag_works_on_an_album_confirmed_as_incomplete(client, cfg, monkeypatch):
    """#133: the tagger refuses file_count < track_count unless told the album is
    knowingly incomplete. /retag never told it, so re-tagging failed for exactly
    the albums a MusicBrainz correction is most likely to affect."""
    d = _make_tagged_album(cfg, "Partial", mbid="rel-part", tagged_at=datetime.now(UTC))
    # One file on disk, but the user confirmed the release has four tracks.
    sc.write(
        d,
        Sidecar(
            mb_release_id="rel-part",
            tagged_at=datetime.now(UTC),
            track_count_expected=4,
        ),
    )
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release", lambda mbid: _release_for_match(mbid, n_tracks=4)
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)

    r = client.post(f"/retag/{_id_for(cfg, d)}")
    assert r.status_code == 200
    assert "Re-tagged" in r.text
    assert "Re-tag failed" not in r.text


def test_retag_still_refuses_an_unconfirmed_short_album(client, cfg, monkeypatch):
    """The control, and the reason this keys on the sidecar rather than on
    counting files: an album the user has NOT confirmed as incomplete must still
    be refused, or the guard against tagging a partial download by accident is
    gone (design §15.3)."""
    d = _make_tagged_album(cfg, "Unconfirmed", mbid="rel-unc", tagged_at=datetime.now(UTC))
    sc.write(d, Sidecar(mb_release_id="rel-unc", tagged_at=datetime.now(UTC)))  # no expectation
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release", lambda mbid: _release_for_match(mbid, n_tracks=4)
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)

    r = client.post(f"/retag/{_id_for(cfg, d)}")
    assert "Re-tag failed" in r.text


def test_album_action_records_activity_against_that_album(client, cfg, monkeypatch):
    """End-to-end for #33: a per-album action tags its activity entry with the
    album's id, so the album's history is queryable (and other albums' events
    aren't mixed in)."""
    from harmonist import activity_store

    d = _make_tagged_album(
        cfg, "Historied", mbid="rel-hist", tagged_at=datetime.now(UTC), item_id=3
    )
    aid = _id_for(cfg, d)
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release", lambda mbid: _release_for_match(mbid, n_tracks=1)
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)
    activity_store.clear()

    r = client.post(f"/retag/{aid}")
    assert r.status_code == 200

    mine = activity_store.recent(album_id=aid)
    assert any("Re-tagged" in e.message for e in mine)
    # Nothing recorded against this album belongs to another one.
    assert all(e.album_id == aid for e in mine)


def test_activity_recent_carries_album_id(cfg):
    """#65: the stored album_id must survive the store → Event conversion, or the
    feed template can't link the entry no matter what the DB holds."""
    from harmonist import activity

    activity.clear()
    activity.record("Tagged something", album_id="rel-xyz", album_label="BoC — Geogaddi")
    activity.record("Something general")

    events = {e.message: (e.album_id, e.album_label) for e in activity.recent(10)}
    assert events["Tagged something"] == ("rel-xyz", "BoC — Geogaddi")
    assert events["Something general"] == (None, None)


def test_activity_feed_links_entries_with_an_album(client, cfg):
    """An entry tied to an album renders a real deep link; one without stays
    plain text (nothing to point at)."""
    from harmonist import activity

    activity.clear()
    activity.record("Unlinked", album_id="rel-geo", album_label="BoC — Geogaddi")
    activity.record("Sync finished")

    body = client.get("/activity").text
    # Only the album NAME is the link — the message stays plain text beside it.
    assert ">BoC — Geogaddi</a>" in body
    assert 'href="/album/rel-geo"' in body
    assert "Unlinked" in body
    # The untied entry is present but carries no album column at all.
    assert "Sync finished" in body
    assert 'href="/album/None"' not in body


def test_status_bar_payload_carries_the_album_name(client, cfg, monkeypatch):
    """#68: the status bar has no album column, so the album name must ride in
    the harmonist-status payload — otherwise it just says "Tagged" with no clue
    which album. Carried as its own field, NOT folded into `details` (that would
    duplicate it in the feed, which is what #65 got wrong)."""
    import json as _json

    d = _make_tagged_album(cfg, "BarNamed", mbid="rel-bar", tagged_at=datetime.now(UTC), item_id=11)
    aid = _id_for(cfg, d)
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release", lambda mbid: _release_for_match(mbid, n_tracks=1)
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)

    r = client.post(f"/retag/{aid}")
    payload = _json.loads(r.headers["HX-Trigger"])["harmonist-status"]
    assert "BarNamed" in payload["album"]
    # ...and it is NOT smuggled into details (the feed renders that verbatim).
    assert "BarNamed" not in (payload["details"] or "")


def test_activity_feed_puts_album_before_message(client, cfg):
    """#68: album name leads, message follows — one left-aligned run. The old
    right-aligned column flung them to opposite edges on a wide window."""
    from harmonist import activity

    activity.clear()
    activity.record("Unlinked now", album_id="rel-x", album_label="The Thamesmen — Gimme")

    body = client.get("/activity").text
    # Single entry, so these are unambiguous positions within the one row.
    assert body.index("The Thamesmen — Gimme") < body.index("Unlinked now")
    assert "text-right" not in body  # no right-aligned column any more


# ---------- The feed poll must not re-send an unchanged feed (#118) ----------


def _feed_version(html: str) -> str:
    m = re.search(r'id="feed-version" data-version="([^"]*)"', html)
    assert m is not None, "no feed-version element — the poll can never match"
    return m.group(1)


def test_an_unchanged_feed_answers_204_and_sends_no_body(client, cfg):
    """The feed polls every 2s. Re-sending every entry each time put 154 KB on
    the wire for a panel that usually hasn't changed, and kept it dimmed by the
    in-flight style for most of the interval (#118). 204 is htmx's documented
    "do not swap"."""
    from harmonist import activity

    activity.clear()
    activity.record("something happened")

    first = client.get("/activity")
    assert first.status_code == 200
    have = _feed_version(first.text)

    again = client.get("/activity", params={"have": have})
    assert again.status_code == 204
    assert again.content == b""


def test_a_new_event_makes_the_feed_answer_200_again(client, cfg):
    """The control: 204 must mean "nothing changed", not "always 204". Without
    this the feed would freeze on whatever it last showed."""
    from harmonist import activity

    activity.clear()
    activity.record("first")
    have = _feed_version(client.get("/activity").text)
    assert client.get("/activity", params={"have": have}).status_code == 204

    activity.record("second")
    r = client.get("/activity", params={"have": have})
    assert r.status_code == 200
    assert "second" in r.text


def test_toggling_technical_detail_is_not_mistaken_for_no_change(client, cfg):
    """The params are part of the version, so a client holding the plain-feed
    token can't be answered 204 when it asks for the audit view."""
    from harmonist import activity, audit

    activity.clear()
    activity.record("visible")
    audit.record("sidecar.update", mbid="a->b")

    have = _feed_version(client.get("/activity").text)
    r = client.get("/activity", params={"have": have, "audit": 1})
    assert r.status_code == 200
    assert "sidecar.update" in r.text


def test_an_unreadable_store_still_carries_a_version(client, broken_store):
    """Otherwise the poll sends an empty `have`, never matches, and re-renders
    the unavailable notice every 2s forever."""
    r = client.get("/activity")
    assert r.status_code == 200
    assert 'id="feed-version"' in r.text


def test_audit_detail_is_capped_but_reports_the_true_total(client, cfg):
    """One action can produce hundreds of rows — the first scan of a large
    library records every album. Rendering them all is what made the payload
    150 KB; the summary still shows the real count (#118)."""
    from harmonist import activity, activity_store, audit
    from harmonist.web.main import AUDIT_DETAIL_LIMIT

    activity.clear()
    n = AUDIT_DETAIL_LIMIT + 15
    with activity_store.action():
        activity.record("Started tracking lots")
        for i in range(n):
            audit.record("album.discovered", album=f"Artist {i}/Album {i}")

    body = client.get("/activity", params={"audit": 1}).text
    assert body.count("album.discovered") == AUDIT_DETAIL_LIMIT
    # The overflow line names the true total, not just what was shown.
    assert f"…and {n - AUDIT_DETAIL_LIMIT} more of {n}" in body


def test_activity_shows_album_label_even_when_link_is_dead(client, cfg):
    """The label is stored with the event, so an entry stays readable after its
    album is gone — the reason we persist it rather than resolving live."""
    from harmonist import activity

    activity.clear()
    activity.record("Tagged", album_id=None, album_label="Departed — Album")

    body = client.get("/activity").text
    assert "Departed — Album" in body  # still named...
    assert 'href="/album/' not in body  # ...but not a link


def test_album_page_shows_history_from_before_the_album_was_re_identified(client, cfg):
    """#103's reason for existing, and the consumer the alias table (#73) was
    built for: an album's records are spread across every id it has ever had.
    Tagging replaces the sidecar's temp_uid with the MBID, so querying the
    current id alone silently drops everything from before — the records most
    likely to explain how the album got into its current state."""
    from harmonist import activity, activity_store

    d = _make_album(cfg, "Historied")
    sc.write(d, Sidecar(store_url="https://x.bandcamp.com/album/h"))
    old_id = _id_for(cfg, d)
    activity_store.clear()
    activity.record("Discovered", album_id=old_id, album_label="Artist — Historied")

    # Tag it: identity moves to the MBID, and the alias is recorded.
    sc.write(d, Sidecar(store_url="https://x.bandcamp.com/album/h", mb_release_id="rel-hist"))
    new_id = _id_for(cfg, d)
    assert new_id != old_id
    activity.record("Tagged", album_id=new_id, album_label="Artist — Historied")

    body = client.get(f"/album/{new_id}").text
    assert "Tagged" in body
    assert "Discovered" in body, "history stopped at the last re-identification"

    # The naive query — current id only — would have missed it entirely.
    naive = [e.message for e in activity_store.recent(50, album_id=new_id)]
    assert "Discovered" not in naive
    assert "Discovered" in [e.message for e in activity_store.album_history(new_id)]


def test_album_page_resolves_a_superseded_id(client, cfg):
    """A link written before the album was re-identified must still land on the
    page rather than 404 — the same forward walk `_find_album` already does."""
    d = _make_album(cfg, "Moved")
    sc.write(d, Sidecar(store_url="https://x.bandcamp.com/album/m"))
    old_id = _id_for(cfg, d)
    sc.write(d, Sidecar(store_url="https://x.bandcamp.com/album/m", mb_release_id="rel-moved"))

    r = client.get(f"/album/{old_id}")
    assert r.status_code == 200
    assert "Moved" in r.text


def test_album_page_404s_for_an_unknown_album(client, cfg):
    _make_album(cfg, "Present")
    assert client.get("/album/no-such-id").status_code == 404


# --- Library → album → Library round-trip (#139) -----------------------------
#
# The three links that have to agree for the trip to land where it started: the
# tile hands the album page its page number, the album page hands it back to the
# index, and the index starts the grid there.


def test_library_tile_tells_the_album_page_which_page_it_came_from(client, cfg):
    _make_library(cfg, 5)
    body = client.get("/library?page=2&limit=2").text
    assert "?from_page=2" in body

    # Page 1 is what Back means with no hint at all, so the common URL stays bare.
    assert "from_page" not in client.get("/library?page=1&limit=2").text


def test_album_page_back_link_returns_to_the_page_it_came_from(client, cfg):
    from datetime import datetime

    d = _make_tagged_album(cfg, "Deep", mbid="rel-deep", tagged_at=datetime.now(UTC), item_id=1)
    aid = _id_for(cfg, d)

    body = client.get(f"/album/{aid}?from_page=4").text
    assert 'href="/?tab=library&page=4"' in body

    # No hint (a bookmark, or a link out of the Activity feed) → the top of the
    # grid, but still the Library tab: arriving from Activity leaves the
    # remembered tab on Inbox, and "Back to library" must not land there.
    plain = client.get(f"/album/{aid}").text
    assert 'href="/?tab=library"' in plain
    assert "page=" not in plain.split("Back to library")[0].rsplit("<a ", 1)[1]


def test_index_renders_the_grid_inline_on_the_page_the_url_names(client, cfg, monkeypatch):
    """The index renders the grid itself rather than fetching it on load, so a
    `?page=` link is answered by the HTML — no follow-up request that would have
    to be told which page it is on, and nothing to drift on a reload or a Back."""
    from harmonist.web import main as web_main

    monkeypatch.setattr(web_main, "_LIBRARY_PAGE_SIZE", 2)
    _make_library(cfg, 5)

    body = client.get("/?tab=library&page=2").text
    assert 'data-page="2"' in body
    assert "Album2" in body and "Album0" not in body
    assert 'harmonistTab("library")' in body


def test_index_ignores_an_unknown_tab_rather_than_reflecting_it(client, cfg):
    """`?tab=` is interpolated into a `panel-<name>` lookup in the tab script."""
    body = client.get("/?tab=</script><script>alert(1)</script>").text
    assert "alert(1)" not in body
    assert "harmonistTab(" in body  # falls back to the remembered tab
    assert "localStorage.getItem('harmonist-tab')" in body


def test_index_defaults_to_the_remembered_tab_and_page_one(client, cfg):
    body = client.get("/").text
    assert "localStorage.getItem('harmonist-tab')" in body
    assert 'data-page="1"' in body


def test_library_pager_links_do_not_inherit_a_competing_page_value(client, cfg):
    """Regression guard for the bug that made every pager click a no-op.

    The refresh wiring used to sit on #library-rows as an `hx-vals`, which htmx
    INHERITS: each pager link inside then sent its own `?page=2` *plus* the
    container's `?page=1`, and the last value won. Keeping the page in each
    fragment's own `hx-get` is what removes the collision, so no ancestor of the
    pager may reintroduce an `hx-vals`."""
    _make_library(cfg, 5)
    index = client.get("/?tab=library").text
    assert "hx-vals" not in index.split('id="library-rows"')[1].split("</div>")[0]

    fragment = client.get("/library?page=2&limit=2").text
    assert "hx-vals" not in fragment
    # Each render re-requests the page it is showing, at the size it is showing,
    # so a refresh mid-browse (every `tasks-changed`) can't yank the user back to
    # the newest page or resize the one they're on.
    assert 'hx-get="/library?page=2&limit=2"' in fragment
    assert "tasks-changed from:body" in fragment


def test_old_deep_link_redirects_to_the_album_page(client, cfg):
    """`?album=<id>` was the deep link before there was an album page (#65). Now
    there is one (#103), so it redirects — activity entries written with the old
    URL are durable, so this will keep arriving indefinitely and must keep
    working."""
    d = _make_album(cfg, "DeepLinked")
    aid = _id_for(cfg, d)

    r = client.get(f"/?album={aid}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/album/{aid}"


def test_deep_link_to_unknown_album_still_renders_page(client, cfg):
    """A stale id (reconcile moved the album's identity, or it left the library)
    must degrade to the normal page plus a notice — never a whole-page 404, and
    never a silent no-op that looks like a broken link."""
    _make_album(cfg, "Present")

    r = client.get("/?album=no-such-album-id")
    assert r.status_code == 200
    assert "isn't in your library any more" in r.text
    # And it stayed put rather than redirecting to a page for an id that
    # resolves to nothing (TestClient follows redirects, so the final URL is
    # the honest check that the #103 redirect didn't fire).
    assert not str(r.url).endswith("/album/no-such-album-id")


def test_action_records_an_id_that_still_resolves_after_tagging(client, cfg, monkeypatch):
    """Regression: the link in the Activity feed must actually work.

    An album that already has a sidecar `temp_uid` — demo's seeded library, or
    anything at all after a restart — is NOT in the in-memory id registry. Tagging
    then DROPS temp_uid in favour of the MBID (models.py), so the pre-mutation id
    the handler was holding is erased from disk and unrecoverable: `_find_album`'s
    registry fallback can't help. Recording that id produced a permanently dead
    link. `_flash_response` now re-derives the id from disk AFTER the mutation.
    """
    from harmonist import activity

    # A sidecar'd but untagged album: its id is the sidecar's temp_uid.
    d = _make_album(cfg, "Relinked")
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/relinked",
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-relink", confidence="exact", file_count=1, track_count=1
            ),
        ),
    )
    aid = _id_for(cfg, d)
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release", lambda mbid: _release_for_match(mbid, n_tracks=1)
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)
    activity.clear()

    # Confirming TAGS it: temp_uid is dropped, the id becomes the MBID.
    assert client.post(f"/confirm/{aid}").status_code == 200
    assert _id_for(cfg, d) != aid  # the identity really did move
    assert sc.read(d).temp_uid is None  # ...and the old id is gone from disk

    entry = next(e for e in activity.recent(10) if "Tagged" in e.message)
    assert entry.album_id is not None
    assert entry.album_label  # the feed can name it
    # The recorded id resolves — i.e. the rendered link is live, not a 404.
    r = client.get(f"/?album={entry.album_id}")
    assert r.status_code == 200
    assert "isn't in your library any more" not in r.text
    # Anchored on the album page's own MB-comparison fetch, which carries the
    # resolved id. (It used to check the retag-refresh span, but that was a
    # modal-only element that leaked onto the page — see the album-retagged
    # gating in library_detail.html.)
    assert f'hx-get="/library/{entry.album_id}/compare"' in r.text


def test_missing_album_notice_is_dismissible_and_clears_the_url(client, cfg):
    """#71: the notice described a navigation that already happened, but sat
    there through tab switches and survived refreshes (?album= stayed in the
    URL and re-rendered it). It now has a dismiss control, and the failure path
    strips the parameter."""
    _make_album(cfg, "Present")

    body = client.get("/?album=no-such-album-id").text
    assert "isn't in your library any more" in body
    assert 'id="deep-link-notice"' in body
    assert 'aria-label="Dismiss"' in body
    assert "searchParams.delete('album')" in body


def test_successful_deep_link_keeps_the_url_parameter(client, cfg):
    """The counterpart to #71: a WORKING deep link must resolve to its album
    rather than being stripped. Only the broken one is.

    Anchored on the re-tag control, which every album page carries whatever its
    state — unlike the MB comparison, which an untagged album has nothing to
    show for."""
    d = _make_album(cfg, "Keeper")
    aid = _id_for(cfg, d)

    body = client.get(f"/?album={aid}").text
    assert f'hx-post="/retag/{aid}"' in body
    assert "searchParams.delete('album')" not in body
    assert 'id="deep-link-notice"' not in body


def test_deep_link_resolves_through_the_alias_chain_after_a_restart(client, cfg):
    """#33's payoff, and the case the path-derived id CANNOT cover.

    id_registry only answers for an album with no sidecar. Once tagging moves an
    album's identity (temp_uid dropped for the MBID), a deep link written under
    the old id has no way home — the old id is erased from disk and isn't
    derivable from the path either. The durable alias chain, recorded at the
    moment of the change, is what rescues it.
    """
    d = _make_album(cfg, "Aliased")
    sc.write(d, Sidecar(store_url="https://x.bandcamp.com/album/aliased"))
    old_id = _id_for(cfg, d)

    # Tag it: identity moves to the MBID and temp_uid is erased from disk.
    sc.write(d, Sidecar(store_url="https://x.bandcamp.com/album/aliased", mb_release_id="rel-al"))
    assert sc.read(d).temp_uid is None
    assert _id_for(cfg, d) == "rel-al"

    r = client.get(f"/?album={old_id}")
    assert r.status_code == 200
    assert "isn't in your library any more" not in r.text
    # Resolved forward to the album's CURRENT id.
    assert 'hx-get="/library/rel-al/compare"' in r.text


def test_post_sync_auto_tag_entry_links_to_the_album(cfg, monkeypatch):
    """#75: the entry a user is most likely to want to click — Harmonist tagged
    something on its own initiative after a sync — was plain text. The id must be
    read back AFTER tagging, since that write moves the album's identity."""
    from harmonist import activity
    from harmonist.tagger import PicardCompatibleTagger
    from harmonist.web.main import _resolve_by_store_url

    d = _make_album(cfg, "AutoTagged")
    sc.write(d, Sidecar(store_url="https://x.bandcamp.com/album/auto"))
    monkeypatch.setattr("harmonist.mb_lookup.lookup_by_bandcamp_url", lambda url: ["rel-auto"])
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release", lambda mbid: _release_for_match(mbid, n_tracks=1)
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)
    activity.clear()

    assert _resolve_by_store_url(d, cfg, PicardCompatibleTagger()) == "tagged"

    entry = next(e for e in activity.recent(10) if "Auto-tagged" in e.message)
    assert entry.album_label == "AutoTagged"  # the on-disk name, as shown before
    # Resolves to the id the album has NOW (the MBID), not its pre-tag temp_uid.
    assert entry.album_id == "rel-auto"
    assert _id_for(cfg, d) == entry.album_id
    # The name is no longer duplicated inside the message.
    assert "AutoTagged" not in entry.message


def test_reconcile_with_nothing_to_do_is_silent_in_the_feed(cfg, caplog):
    """#101: reconcile runs on startup and after every sync, and used to report a
    no-op with three entries — making "nothing happened" the feed's most frequent
    content. It stays in the server log so it's still greppable."""
    import logging

    from harmonist import activity
    from harmonist.web.reconcile_runner import reconcile_pending_orphans

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    activity.clear()

    with caplog.at_level(logging.INFO):
        reconcile_pending_orphans(cfg.paths.music_dir, fetch_urls=lambda m: [], albums=[])

    assert activity.recent(10) == []
    assert any("nothing to reconcile" in r.message for r in caplog.records)


def test_reconcile_with_work_announces_once(cfg):
    """One start line naming the count, then the summary — not a separate
    "started" and "N to check"."""
    from harmonist import activity
    from harmonist.web.reconcile_runner import reconcile_pending_orphans

    _make_album(cfg, "NeedsReconcile", mbid="rel-nr")
    activity.clear()

    reconcile_pending_orphans(cfg.paths.music_dir, fetch_urls=lambda m: [])

    msgs = [e.message for e in activity.recent(10)]
    starts = [m for m in msgs if m.startswith("Reconcile started")]
    assert len(starts) == 1, f"expected exactly one start line, got {starts}"
    assert "1 album(s) to check" in starts[0]
    assert any(m.startswith("Reconcile done") for m in msgs)


def test_reconcile_entries_link_to_their_album(cfg):
    """#75: reconcile's 'New -> ...' entries carry the album too, with the same
    'Artist — Title' column the rest of the feed uses."""
    from harmonist import activity
    from harmonist.web.reconcile_runner import reconcile_pending_orphans

    d = _make_album(cfg, "Reconciled", mbid="rel-rec")
    activity.clear()

    reconcile_pending_orphans(cfg.paths.music_dir, fetch_urls=lambda mbid: [])

    entry = next(e for e in activity.recent(20) if "New →" in e.message)
    # The recorded id is the one the album actually has after reconcile wrote its
    # sidecar — i.e. the link resolves, rather than merely being non-empty.
    assert entry.album_id == _id_for(cfg, d)
    assert entry.album_label and "Reconciled" in entry.album_label
    assert "Reconciled" not in entry.message  # not duplicated in the text


def test_deep_link_follows_moved_album_id(client, cfg):
    """The id in an old activity row is a snapshot. When a NEW album's registry
    UUID is superseded (a sidecar write giving it an MBID identity), the deep
    link must still resolve via the registry fallback rather than 404."""
    d = _make_album(cfg, "Moved")
    original_id = _id_for(cfg, d)  # registry UUID, minted for the NEW album
    sc.write(d, Sidecar(mb_release_id="rel-moved"))
    current_id = _id_for(cfg, d)
    assert current_id != original_id  # identity moved to the MBID

    r = client.get(f"/?album={original_id}")
    assert r.status_code == 200
    # Resolved forward to the album's CURRENT id, so the fragment fetch works.
    assert f'hx-get="/library/{current_id}/compare"' in r.text


def test_retag_emits_album_retagged_trigger(client, cfg, monkeypatch):
    """On success /retag fires an `album-retagged` HX-Trigger so the open detail
    modal reloads itself — otherwise its disk-vs-MB comparison stays stale (#34)."""
    d = _make_tagged_album(
        cfg, "TriggerRetag", mbid="rel-1", tagged_at=datetime.now(UTC), item_id=1
    )
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release", lambda mbid: _release_for_match(mbid, n_tracks=1)
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)
    r = client.post(f"/retag/{_id_for(cfg, d)}")
    assert r.status_code == 200
    assert "album-retagged" in r.headers.get("HX-Trigger", "")


def test_retag_failure_does_not_emit_refresh_trigger(client, cfg, monkeypatch):
    """A failed re-tag must not fire album-retagged (nothing changed to refresh)."""
    d = _make_tagged_album(cfg, "FailRetag", mbid="rel-1", tagged_at=datetime.now(UTC), item_id=1)

    def boom(mbid):
        raise RuntimeError("mb down")

    monkeypatch.setattr("harmonist.mb_lookup.fetch_release", boom)
    r = client.post(f"/retag/{_id_for(cfg, d)}")
    assert r.status_code == 200  # rendered as a flash, not an HTTP error
    assert "Re-tag failed" in r.text
    assert "album-retagged" not in r.headers.get("HX-Trigger", "")


def test_album_page_wires_retag_progress_and_refresh(client, cfg):
    """The re-tag button shows a tagging indicator, disables the whole action
    group while it runs, and refreshes the page when it lands.

    The page must NOT carry the modal's `album-retagged` listener: that fetches
    the detail fragment into #modal, which base.html opens as a dialog — so
    re-tagging from the page popped up the dialog the page replaced."""
    d = _make_tagged_album(cfg, "DetailWiring", mbid="rel-1", tagged_at=datetime.now(UTC))
    aid = _id_for(cfg, d)
    r = client.get(f"/album/{aid}")
    assert r.status_code == 200
    assert f'hx-indicator="#tagging-{aid}"' in r.text  # progress spinner target
    assert f'id="tagging-{aid}"' in r.text  # the overlay itself
    # `this`, not a group selector: htmx disables matching elements before it
    # issues the request, so a selector covering the trigger stops the request
    # firing at all. The overlay is what blocks the other buttons.
    assert 'hx-disabled-elt="this"' in r.text
    # The page reloads itself, and the modal's refresh listener stays out.
    # Asserted on the listener, not on `hx-target="#modal"` — the header's
    # Bandcamp-setup button legitimately targets the modal on every page.
    assert "album-retagged from:body" not in r.text
    assert "window.location.reload()" in r.text


def test_album_page_dates_are_relative_with_the_exact_value_on_hover(client, cfg):
    """Dates read as elapsed time, since what they answer is "how recently?",
    with the exact timestamp on hover so nothing is lost.

    (The title used to be a link through to this page; on the page itself
    there's nowhere to go, and since #129 there is no other view.)"""
    d = _make_tagged_album(cfg, "Linked", mbid="rel-l", tagged_at=datetime.now(UTC))
    body = client.get(f"/album/{_id_for(cfg, d)}").text

    assert "just now" in body  # relative, not a bare date
    assert datetime.now(UTC).strftime("%Y-%m-%d") in body  # exact value on hover


def test_leaving_the_library_returns_you_to_it(client, cfg):
    """#129: rematch and Forget both send the album OUT of the Library — to
    Needs MBID and to NEW. The dialog used to close on success, which took you
    back by construction; a page has to navigate, or it sits there describing an
    album that moved."""
    d = _make_tagged_album(cfg, "Departing", mbid="rel-s", tagged_at=datetime.now(UTC))
    body = client.get(f"/album/{_id_for(cfg, d)}").text

    assert body.count("window.location.href = '/'") == 2  # rematch and Forget
    # No control still tries to close a dialog. Asserted on the CALL SITE, not
    # the name: base.html still defines harmonistCloseModal for the Bandcamp
    # setup modal, which is a different dialog and stays.
    assert "successful) harmonistCloseModal()" not in body
    # …and both controls are actually there to navigate from.
    aid = _id_for(cfg, d)
    assert f"/library/{aid}/rematch" in body
    assert f"/forget/{aid}" in body


def test_retag_recomputes_count_and_promotes_incomplete_to_complete(client, cfg, monkeypatch):
    """When MB over-counts (e.g. a phantom album-mix track) an album lands as a
    spurious INCOMPLETE. After the user fixes MB upstream, Re-tag from MB
    recomputes track_count_expected from the corrected release and the state
    self-corrects to COMPLETE — no explicit override needed."""
    from harmonist import scanner
    from harmonist.models import AlbumState

    d = _make_album(cfg, "OverCounted", mbid="rel-1")  # 1 file, tagged rel-1
    sc.write(
        d,
        Sidecar(
            mb_release_id="rel-1",
            track_count_expected=2,  # MB wrongly said 2 → 1 file < 2 → INCOMPLETE
            tagged_at=datetime.now(UTC),
        ),
    )
    before = next(a for a in scanner.scan(cfg.paths.music_dir) if a.path == d)
    assert before.state == AlbumState.INCOMPLETE  # precondition

    # MB corrected upstream: the release now has 1 track, matching the 1 file.
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release",
        lambda mbid: _release_for_match(mbid, n_tracks=1),
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)

    r = client.post(f"/retag/{_id_for(cfg, d)}")
    assert r.status_code == 200

    assert sc.read(d).track_count_expected == 1  # recomputed from corrected MB
    after = next(a for a in scanner.scan(cfg.paths.music_dir) if a.path == d)
    assert after.state == AlbumState.COMPLETE  # self-corrected


def test_retag_400_when_no_mbid_on_sidecar(client, cfg):
    d = _make_album(cfg, "NoMBID")
    sc.write(d, Sidecar())
    aid = _id_for(cfg, d)
    r = client.post(f"/retag/{aid}")
    assert r.status_code == 400


def test_reconcile_is_idempotent_on_already_reconciled_album(client, cfg, monkeypatch):
    """After Forget → auto-reconcile, a stale 'Reconcile from tags' click
    used to 400. It should now return a calm 'already reconciled' message.
    """
    from harmonist.models import BandcampInfo, Sidecar

    d = _make_album(cfg, "ToReconcile")
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=None),
            mb_release_id="rel-x",
        ),
    )
    aid = _id_for(cfg, d)
    r = client.post(f"/reconcile/{aid}")
    assert r.status_code == 200, r.text
    assert "already reconciled" in r.text.lower() or "reconcile" in r.text.lower()


def test_reconcile_returns_warning_when_no_mbid_atom(client, cfg):
    """Untagged new album should get a clear no-MBID message, not a 400."""
    d = _make_album(cfg, "Untagged")  # no MBID atom on file
    aid = _id_for(cfg, d)
    r = client.post(f"/reconcile/{aid}")
    assert r.status_code == 200
    assert "MusicBrainz Album Id" in r.text or "no" in r.text.lower()


def test_forget_deletes_sidecar(client, cfg):
    from datetime import datetime

    d = _make_tagged_album(cfg, "Forgetme", mbid="rel-1", tagged_at=datetime.now(UTC), item_id=1)
    assert sc.has_sidecar(d)
    aid = _id_for(cfg, d)
    r = client.post(f"/forget/{aid}")
    assert r.status_code == 200
    assert "Forgotten" in r.text
    # Sidecar gone; album files untouched
    assert not sc.has_sidecar(d)
    assert (d / "01 Track.m4a").exists()


def test_forget_adds_path_to_exemption_set(client, cfg):
    """Forget must add the album's path to forgotten_paths so the
    auto-reconciler won't undo the user's intent on the next /tasks tick.
    """
    from datetime import datetime

    d = _make_tagged_album(cfg, "Exempt", mbid="rel-1", tagged_at=datetime.now(UTC), item_id=1)
    aid = _id_for(cfg, d)
    client.post(f"/forget/{aid}")
    assert d in client.app.state.forgotten_paths


def test_explicit_reconcile_clears_exemption(client, cfg):
    """Explicit /reconcile/{id} should discard any prior Forget exemption —
    the user's most-recent intent wins.
    """
    d = _make_album(cfg, "Cleared")
    # Pre-seed the album in the exemption set
    client.app.state.forgotten_paths.add(d)
    # The album has no sidecar (new) and no MBID atom — reconcile is a
    # no-op but the route still discards the exemption.
    aid = _id_for(cfg, d)
    r = client.post(f"/reconcile/{aid}")
    assert r.status_code == 200
    assert d not in client.app.state.forgotten_paths


def test_cover_serves_when_present(client, cfg):
    d = _make_album(cfg, "WithCover")
    (d / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0FAKE")
    aid = _id_for(cfg, d)
    r = client.get(f"/cover/{aid}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content.startswith(b"\xff\xd8")


# ---------- album id stability ----------


def test_new_album_id_is_minted_from_registry(client, cfg):
    """A NEW album (no sidecar) gets a UUID from the in-process registry;
    the same path gets the same id on repeat scans.
    """
    d = _make_album(cfg, "NewOne")
    aid1 = _id_for(cfg, d)
    aid2 = _id_for(cfg, d)
    assert aid1 == aid2
    # No sidecar exists, so the id can't have come from one
    assert not sc.has_sidecar(d)
    # 32 hex chars = uuid4().hex
    assert len(aid1) == 32


def test_sidecar_album_id_matches_temp_uid(client, cfg):
    """A sidecar'd album's id is the sidecar's temp_uid."""
    d = _make_album(cfg, "WithSidecar")
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/y",
        ),
    )
    aid = _id_for(cfg, d)
    assert aid == sc.read(d).temp_uid


def test_sidecar_album_id_matches_mbid_when_tagged(client, cfg):
    """A tagged album's id is its MBID."""
    from datetime import datetime

    d = _make_tagged_album(
        cfg, "Tagged", mbid="abc-mbid-1234", tagged_at=datetime.now(UTC), item_id=42
    )
    aid = _id_for(cfg, d)
    assert aid == "abc-mbid-1234"


def test_new_album_id_survives_first_sidecar_write(client, cfg):
    """The registry UUID minted for a NEW album is reused when the first
    sidecar is written — so the inbox URL the user interacted with stays
    valid across the NEW → sidecar'd transition.
    """
    d = _make_album(cfg, "Surviving")
    registry_uid = _id_for(cfg, d)  # mints into registry as a side effect

    # Now write a sidecar. The temp_uid should be the registry value.
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/y",
        ),
    )
    assert sc.read(d).temp_uid == registry_uid
    # And the album.id from a fresh scan still matches
    assert _id_for(cfg, d) == registry_uid


def test_sidecar_album_id_survives_rename(client, cfg):
    """The UUID lives in the sidecar JSON, which moves with the directory
    on rename. So album.id is stable across renames for sidecar'd albums.
    """
    d = _make_album(cfg, "RenameMe")
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/y",
        ),
    )
    aid_before = _id_for(cfg, d)

    new_d = d.parent / "Renamed"
    d.rename(new_d)
    aid_after = _id_for(cfg, new_d)
    assert aid_before == aid_after


def test_route_404_when_id_is_unknown(client):
    r = client.post("/recheck/this-id-doesnt-exist")
    assert r.status_code == 404


# ---------- Confirm as Incomplete (§15.3) ----------


def test_confirm_incomplete_tags_and_persists_expected_count(client, cfg, monkeypatch):
    """POST /confirm/{id}/incomplete tags via the incomplete-mode tagger,
    sets mb_release_id and track_count_expected, and leaves the album in
    INCOMPLETE on next scan.
    """
    d = _make_album(cfg, "ShortAlbum")  # 1 file from the fixture
    # Seed a candidate with file_count=1, track_count=3 (MB says 3, disk has 1)
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/y",
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-mbid",
                confidence="approximate",
                file_count=1,
                track_count=3,
            ),
        ),
    )

    def fake_release(mbid):
        return {
            "id": mbid,
            "title": "Three-track Album",
            "release-group": {"id": "rg-1"},
            "medium-list": [
                {
                    "position": "1",
                    "track-list": [
                        {
                            "id": "rt-1",
                            "position": "1",
                            "title": "T1",
                            "recording": {"id": "rec-1", "title": "T1"},
                        },
                        {
                            "id": "rt-2",
                            "position": "2",
                            "title": "T2",
                            "recording": {"id": "rec-2", "title": "T2"},
                        },
                        {
                            "id": "rt-3",
                            "position": "3",
                            "title": "T3",
                            "recording": {"id": "rec-3", "title": "T3"},
                        },
                    ],
                }
            ],
        }

    monkeypatch.setattr("harmonist.mb_lookup.fetch_release", fake_release)
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)

    aid = _id_for(cfg, d)
    r = client.post(f"/confirm/{aid}/incomplete")
    assert r.status_code == 200
    assert "incomplete" in r.text.lower()

    loaded = sc.read(d)
    assert loaded.mb_release_id == "rel-mbid"
    assert loaded.track_count_expected == 3
    assert loaded.mb_match_candidate is None
    assert loaded.tagged_at is not None

    # Scanner now reports INCOMPLETE
    from harmonist import scanner

    a = next(a for a in scanner.scan(cfg.paths.music_dir) if a.path == d)
    from harmonist.models import AlbumState

    assert a.state == AlbumState.INCOMPLETE


def test_confirm_mistag_adopts_owned_store_url(client, cfg, monkeypatch):
    """Confirming a mis-tag re-tags the MBID AND adopts the owned edition's
    purchase URL as store_url — otherwise the album keeps the wrong-edition URL,
    matches no purchase on the next sync, and falls through to surrender."""
    d = _make_album(cfg, "Mistagged")
    sc.write(
        d,
        Sidecar(
            # The WRONG edition's URL the album currently carries.
            store_url="https://ultimae.bandcamp.com/album/life-24bits-2",
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-standard",
                confidence="exact",
                file_count=1,
                track_count=1,
                # Mis-tag provenance: the owned edition + the URL it was bought at.
                mistag_owned_url="https://ultimae.bandcamp.com/album/life",
                mistag_owned_label="ASURA / Life²",
            ),
        ),
    )
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release",
        lambda mbid: _release_for_match(mbid, n_tracks=1),
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)

    aid = _id_for(cfg, d)
    r = client.post(f"/confirm/{aid}")
    assert r.status_code == 200
    loaded = sc.read(d)
    assert loaded.mb_release_id == "rel-standard"
    # store_url now points at the edition the user actually purchased.
    assert loaded.store_url == "https://ultimae.bandcamp.com/album/life"


def test_confirm_normal_candidate_keeps_store_url(client, cfg, monkeypatch):
    """A non-mis-tag candidate has no owned URL, so confirming keeps the
    existing store_url unchanged."""
    d = _make_album(cfg, "NormalConfirm")
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/keep-me",
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-keep",
                confidence="exact",
                file_count=1,
                track_count=1,
            ),
        ),
    )
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release",
        lambda mbid: _release_for_match(mbid, n_tracks=1),
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)
    aid = _id_for(cfg, d)
    client.post(f"/confirm/{aid}")
    assert sc.read(d).store_url == "https://x.bandcamp.com/album/keep-me"


def test_confirm_incomplete_400_without_candidate(client, cfg):
    d = _make_album(cfg, "NoCandidate")
    sc.write(d, Sidecar())
    aid = _id_for(cfg, d)
    r = client.post(f"/confirm/{aid}/incomplete")
    assert r.status_code == 400


def test_needs_review_card_offers_incomplete_when_file_count_short(client, cfg):
    """The Confirm as Incomplete button appears on a suggestion card
    where file_count < track_count — and not when they're equal.
    """
    d_short = _make_album(cfg, "ShortMatch")
    sc.write(
        d_short,
        Sidecar(
            store_url="https://x.bandcamp.com/album/short",
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-short",
                confidence="approximate",
                file_count=1,
                track_count=3,
            ),
        ),
    )
    d_exact = _make_album(cfg, "ExactMatch")
    sc.write(
        d_exact,
        Sidecar(
            store_url="https://x.bandcamp.com/album/exact",
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-exact",
                confidence="approximate",
                file_count=1,
                track_count=1,
            ),
        ),
    )
    r = client.get("/tasks")
    # Button text appears only when there's at least one short album
    assert "Confirm as Incomplete" in r.text
    # Count occurrences — exactly one (only on the short card, not the exact one)
    assert r.text.count("Confirm as Incomplete") == 1


def test_library_shows_partial_tag_badge(client, cfg):
    """An album with some files missing the MBID atom surfaces a
    '{N}/{M} tagged' badge alongside the title in the library row.
    """
    from datetime import datetime

    d = _make_album(cfg, "PartiallyTagged")
    # Add a second file
    second = d / "02 Other.m4a"
    shutil.copy(SINE_M4A, second)
    # Tag only the first
    audio = MP4(d / "01 Track.m4a")
    audio[ATOM_MB_ALBUM_ID] = [b"rel-aaa"]
    audio.save()
    sc.write(
        d,
        Sidecar(
            mb_release_id="rel-aaa",
            tagged_at=datetime.now(UTC),
        ),
    )
    r = client.get("/library")
    assert r.status_code == 200
    assert "PartiallyTagged" in r.text
    assert "1/2 tagged" in r.text


def test_library_includes_incomplete_albums(client, cfg):
    """Library shows both COMPLETE and INCOMPLETE — both are terminal."""
    from datetime import datetime

    _make_tagged_album(
        cfg,
        "Whole",
        mbid="rel-c",
        tagged_at=datetime.now(UTC),
        item_id=1,
    )
    d_partial = _make_album(cfg, "Partial")
    audio = MP4(d_partial / "01 Track.m4a")
    audio[ATOM_MB_ALBUM_ID] = [b"rel-i"]
    audio.save()
    sc.write(
        d_partial,
        Sidecar(
            mb_release_id="rel-i",
            tagged_at=datetime.now(UTC),
            track_count_expected=5,
        ),
    )
    r = client.get("/library")
    assert r.status_code == 200
    assert "Whole" in r.text
    assert "Partial" in r.text
    # INCOMPLETE row shows the "N of M" badge
    assert "1 of 5" in r.text


def test_canonical_id_change_mid_transaction(client, cfg, monkeypatch):
    """A handler that mutates an album to assign an MBID changes the
    canonical id as a side effect. The action response itself is 200 (the
    handler ran at the URL that was canonical at request time); the new
    canonical id takes effect on the *next* request via the inbox refresh.

    Design: we do NOT redirect mid-transaction. The HX-Trigger='tasks-changed'
    header propagates the new id to the UI.
    """
    d = _make_album(cfg, "ToAssign")
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/y",
        ),
    )
    temp_uid = sc.read(d).temp_uid
    mbid = "abc12345-1234-1234-1234-1234567890ab"

    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release",
        lambda mbid: _release_for_match(mbid, n_tracks=1),
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)

    # POST at the temp_uid URL — runs the handler, returns 200
    r = client.post(f"/manual/{temp_uid}/assign", data={"mbid": mbid})
    assert r.status_code == 200
    assert "tasks-changed" in r.headers.get("hx-trigger", "")

    # Sidecar identity flipped: MBID set, temp_uid dropped
    loaded = sc.read(d)
    assert loaded.mb_release_id == mbid
    assert loaded.temp_uid is None

    # Album's canonical id is now the MBID
    assert _id_for(cfg, d) == mbid

    # The old temp_uid URL STILL resolves — to the same album. This assertion was
    # inverted by #33: it previously expected 404, which encoded the limitation of
    # the in-memory registry (empty for anything already sidecar'd, and after any
    # restart) rather than a design goal. `_find_album` has always intended the
    # opposite — "404 only when we can't resolve the id any way" — and the durable
    # alias chain now makes that achievable. The id refers to the same album, so
    # acting on it is correct; a stale page's buttons keep working instead of
    # dead-ending.
    r_stale = client.post(f"/recheck/{temp_uid}")
    assert r_stale.status_code == 200


# ---------- activity feed ----------


def test_activity_pages_backwards_through_history(client, cfg):
    """#14: the feed was capped at 100 with no way to reach anything older, so
    the durable history was accumulating unreachably."""
    from harmonist import activity

    activity.clear()
    for i in range(120):
        activity.record(f"event {i:03d}")

    # Newest first, so page 1 is 119 down to 070.
    first = client.get("/activity?limit=50").text
    assert "event 119" in first
    assert "event 070" in first  # 50th and last on the page
    assert "event 069" not in first  # page boundary
    assert "Load older activity" in first

    second = client.get("/activity?offset=50&limit=50").text
    assert "event 070" not in second  # no overlap with page 1
    assert "event 069" in second  # continues exactly where page 1 stopped
    assert "event 020" in second
    # A later page is an append-only fragment: no list wrapper, no controls.
    assert "<ul id=" not in second
    assert "Technical detail" not in second


def test_activity_last_page_has_no_load_more(client, cfg):
    """has_more comes from asking for limit+1, so the final page must not offer
    another — a COUNT would be wasted work on an unbounded table."""
    from harmonist import activity

    activity.clear()
    for i in range(10):
        activity.record(f"event {i}")

    body = client.get("/activity?limit=50").text
    assert "Load older activity" not in body


def test_activity_audit_toggle_reveals_unscoped_records(client, cfg):
    """The point of the toggle: audit rows written OUTSIDE an action scope have
    no action_id, so no entry's "what changed" disclosure can show them. Without
    this they're reachable from nowhere in the UI."""
    from harmonist import activity, activity_store, audit

    activity_store.clear()
    activity.record("a user-facing outcome")
    audit.record("unscoped.bookkeeping", detail="no action scope")

    plain = client.get("/activity").text
    assert "a user-facing outcome" in plain
    assert "unscoped.bookkeeping" not in plain  # invisible by default

    with_audit = client.get("/activity?audit=1").text
    assert "a user-facing outcome" in with_audit
    assert "unscoped.bookkeeping" in with_audit


def test_audit_detail_hangs_off_its_entry_and_only_when_asked(client, cfg):
    """Detail belongs to the entry that caused it, and appears only with "Show
    details" on. It used to be an always-present <details> disclosure as well as
    an inline interleave — two routes to the same records, and the interleave is
    what let one big action swamp the page (#123)."""
    from harmonist import activity, activity_store, audit

    activity_store.clear()
    with activity_store.action():
        activity.record("Tagged", album_id="rel-1", album_label="An Album")
        audit.record("sidecar.update", album_id="rel-1", mbid="a->b")

    off = client.get("/activity").text
    assert "Tagged" in off
    assert "sidecar.update" not in off
    assert "what changed" not in off  # the disclosure is gone entirely

    on = client.get("/activity?audit=1").text
    assert "Tagged" in on
    assert on.count("sidecar.update") == 1  # once, under its entry — not twice


def test_audit_rows_outside_an_action_still_appear(client, cfg):
    """They have no entry to hang off, so grouping alone would hide them.

    Most writes are scoped (the HTTP middleware wraps every mutating request;
    reconcile scopes per album), but the post-sync surrender / unmatched-purchase
    pass runs on the sync thread outside any request — and those rows are the
    only record that a surrender happened (#123)."""
    from harmonist import activity, activity_store, audit

    activity_store.clear()
    activity.record("Reconcile done")
    audit.record("sidecar.create", album_id="rel-1", mbid="x")  # no action scope

    assert "sidecar.create" not in client.get("/activity").text
    on = client.get("/activity?audit=1").text
    assert "sidecar.create" in on
    assert "Reconcile done" in on


def test_a_big_action_cannot_swamp_the_feed(client, cfg):
    """The bug behind #123: audit rows were interleaved by id, so one action that
    wrote 970 of them filled page 1 with identical lines and pushed every outcome
    off it. Detail hanging off its entry is what bounds that."""
    from harmonist import activity, activity_store, audit
    from harmonist.web.main import AUDIT_DETAIL_LIMIT

    activity_store.clear()
    for i in range(5):
        activity.record(f"Bandcamp sync finished — {i} new items")
    with activity_store.action():
        activity.record("Started tracking 300 albums")
        for i in range(300):
            audit.record("album.discovered", album=f"Artist {i}/Album {i}")

    body = client.get("/activity?audit=1").text
    # Every ordinary outcome is still on the page…
    for i in range(5):
        assert f"Bandcamp sync finished — {i} new items" in body
    # …and the 300 are capped rather than filling it.
    assert body.count("album.discovered") <= AUDIT_DETAIL_LIMIT


def test_activity_empty_state(client):
    r = client.get("/activity")
    assert r.status_code == 200
    assert "No activity yet" in r.text


def test_activity_lists_recorded_events(client):
    from harmonist import activity

    activity.record("Tagged — Some Album", Level.INFO)
    activity.record("Sync failed — boom", Level.ERROR)
    r = client.get("/activity")
    assert r.status_code == 200
    assert "Tagged — Some Album" in r.text
    assert "Sync failed — boom" in r.text
    # No empty-state copy when there are events
    assert "No activity yet" not in r.text


def test_action_outcome_recorded_to_activity(client, cfg):
    """A flash-producing action also lands in the activity feed."""
    d = _make_album(cfg, "ActivityAlbum")
    client.post(f"/reconcile/{_id_for(cfg, d)}")
    r = client.get("/activity")
    assert "ActivityAlbum" in r.text or "Reconcile" in r.text or "reconcile" in r.text


def test_index_has_activity_tab(client):
    r = client.get("/")
    assert 'data-tab="activity"' in r.text
    assert "Activity" in r.text


# ---------- settings ----------


def test_settings_page_renders(client, cfg):
    r = client.get("/settings")
    assert r.status_code == 200
    assert "Settings" in r.text
    # read-only paths + editable fields present
    assert "Music library" in r.text
    assert 'name="download_format"' in r.text
    assert 'name="max_downloads_per_sync"' in r.text
    assert 'name="user_agent"' in r.text
    # The preferences form must submit via HTMX (hx-boost) so the CSRF
    # middleware's required HX-Request header is sent — a plain POST 403s.
    assert 'action="/settings" hx-boost="true"' in r.text


def _button_tag(html: str, button_id: str) -> str:
    """The opening <button> tag with this id, so a test can assert on the
    attributes of THAT button rather than anywhere in the page."""
    m = re.search(rf'<button id="{button_id}"[^>]*>', html)
    assert m is not None, f"no <button id={button_id}> in page"
    return m.group(0)


def test_settings_page_disables_the_global_sync_actions(client, cfg):
    """Sync is inert on Settings: it would read configuration the user is
    partway through changing (#108)."""
    cfg.cookies_file.write_text(
        "# Netscape HTTP Cookie File\n.bandcamp.com\tTRUE\t/\tFALSE\t0\tident\tabc\n"
    )
    settings = client.get("/settings")
    assert "disabled" in _button_tag(settings.text, "sync-button")
    # Control: the same header on the index page is live, or this test would
    # pass against a sync button that is disabled everywhere.
    assert "disabled" not in _button_tag(client.get("/").text, "sync-button")


def test_sync_popover_is_bound_to_the_sync_button(client, cfg):
    """The popover's submit is a second sync trigger, so it must not open over
    a disabled button. That binding is a CSS rule keyed on #sync-popover /
    #sync-button, so all pytest can check is that the two hooks it needs are
    present — whether the popover actually stays shut is an e2e test (#110)."""
    cfg.cookies_file.write_text(
        "# Netscape HTTP Cookie File\n.bandcamp.com\tTRUE\t/\tFALSE\t0\tident\tabc\n"
    )
    home = client.get("/").text
    assert 'id="sync-popover"' in home
    assert 'id="sync-button"' in home
    css = Path(__file__).resolve().parents[1] / "static" / "harmonist.css"
    assert "#sync-control:has(#sync-button:disabled) #sync-popover" in css.read_text()


def test_settings_page_disables_the_header_setup_button(client, cfg):
    """The header's Set-up duplicate goes inert too — Settings has its own in
    the Bandcamp sync section, so this is no dead end (#108)."""
    settings = client.get("/settings")  # no cookies → header offers setup
    assert "disabled" in _button_tag(settings.text, "bandcamp-setup-button")
    # The escape hatch: Settings' own control, still live.
    assert 'hx-get="/bandcamp/setup"' in settings.text
    assert "disabled" not in _button_tag(client.get("/").text, "bandcamp-setup-button")


def test_debug_memory_endpoint(client):
    r = client.get("/debug/memory")
    assert r.status_code == 200
    data = r.json()
    assert "rss_mb" in data
    assert "albums_in_snapshot" in data
    assert "scan_cache_entries" in data
    assert "gc_counts" in data
    # tracemalloc off by default → null payload + the enable hint.
    assert data["tracemalloc"] is None
    assert "tracemalloc_hint" in data


def test_settings_save_persists_and_applies_live(client, cfg):
    r = client.post(
        "/settings",
        data={
            "download_format": "alac",
            "max_downloads_per_sync": "12",
            "user_agent": "Harmonist/9.9 ( me@example.com )",
            "cover_art_size": "500",
            "log_level": "warning",
        },
    )
    assert r.status_code == 200
    assert "Settings saved" in r.text
    # Applied live on the running app
    live = client.app.state.cfg
    assert live.bandcamp.download_format == "alac"
    assert live.bandcamp.max_downloads_per_sync == 12
    assert live.cover_art.size == "500"
    assert live.log_level == "warning"
    # Persisted to harmonist.toml (round-trips on next load)
    toml = (cfg.paths.config_dir / "harmonist.toml").read_text()
    assert "alac" in toml
    assert "max_downloads_per_sync = 12" in toml


def test_settings_save_rejects_invalid_cover_size(client, cfg):
    r = client.post(
        "/settings",
        data={
            "download_format": "flac",
            "max_downloads_per_sync": "5",
            "user_agent": "Harmonist/0.1 ( x@y.z )",
            "cover_art_size": "999",  # not a valid Literal
            "log_level": "info",
        },
    )
    assert r.status_code == 200
    assert "Couldn't save" in r.text
    # nothing persisted
    assert not (cfg.paths.config_dir / "harmonist.toml").exists()


def test_tasks_shows_scanning_placeholder_while_scanning(client, cfg, monkeypatch):
    """While the background scan is in progress and the snapshot is empty, the
    inbox shows a 'Scanning…' placeholder with live counts (not 'Inbox empty')."""
    runner = client.app.state.scan_runner
    monkeypatch.setattr(
        runner,
        "status",
        lambda: {
            "state": "scanning",
            "dirs_scanned": 42,
            "albums_found": 10,
            "started_at": None,
            "finished_at": None,
            "last_error": None,
        },
    )
    r = client.get("/tasks")  # no albums on disk → empty snapshot
    assert "Scanning your library" in r.text
    assert "42" in r.text
    assert "directories" in r.text
    assert "Inbox is empty" not in r.text


def test_engaged_lifespan_serves_background_scan_snapshot(cfg):
    """With the lifespan running (TestClient as a context manager), the
    background scanner engages and routes serve its snapshot — not a
    request-time scan. The /scan/status endpoint reports progress.

    Startup also auto-kicks a reconcile pass the moment the first scan
    completes, and /tasks deliberately replaces the album cards with a
    "Sorting your library…" progress panel while reconcile is running — so
    asserting on /tasks right after the scan reports "done" raced that pass
    (the issue #52 flake: near-instant locally, but a late-scheduled thread
    on a loaded CI runner left it mid-pass). Wait for full startup
    quiescence — scan done AND the reconcile pass finished — first."""
    import time as _time

    _make_album(cfg, "BgAlbum")  # present before startup → initial scan finds it
    with TestClient(create_app(cfg), headers={"HX-Request": "true"}) as client:
        status: dict = {}
        reconcile: dict = {}
        for _ in range(200):
            status = client.get("/scan/status").json()
            reconcile = client.get("/reconcile/status").json()
            # finished_at set ⇒ the startup reconcile ran to completion (its
            # initial state is also "idle", so "idle" alone can't tell
            # not-yet-started from done).
            if (
                status["state"] == "done"
                and reconcile["state"] == "idle"
                and (reconcile["finished_at"] is not None)
            ):
                break
            _time.sleep(0.02)
        assert status["state"] == "done"
        assert status["albums_found"] == 1
        assert reconcile["state"] == "idle" and reconcile["finished_at"] is not None
        # /tasks serves the snapshot the background scan produced.
        assert "BgAlbum" in client.get("/tasks").text


def test_app_attribute_is_memoized(monkeypatch):
    """`harmonist.web.main.app` builds the app once and caches it — repeated
    access (uvicorn does this at startup) must not run create_app() twice."""
    import harmonist.web.main as m

    calls: list = []

    def fake_create_app():
        calls.append(1)
        return object()

    monkeypatch.setattr(m, "create_app", fake_create_app)
    monkeypatch.setattr(m, "_app_singleton", None)
    first = m.app
    second = m.app
    assert first is second
    assert len(calls) == 1


def test_erase_sidecars_is_audited_per_album(client, cfg):
    """Erasing sidecars is the most destructive thing Harmonist does — every
    album's identity, match candidate and purchase link at once — and it had no
    audit record. One line per sidecar names which albums lost one; a bare count
    would say a nuke happened but not what it took."""
    from harmonist import activity_store
    from harmonist import sidecar as scmod
    from harmonist.activity_store import Source

    a = _make_album(cfg, "NukedOne")
    b = _make_album(cfg, "NukedTwo")
    scmod.write(a, Sidecar(mb_release_id="rel-a"))
    scmod.write(b, Sidecar(mb_release_id="rel-b"))
    activity_store.clear()

    assert client.post("/settings/erase-sidecars").status_code == 200

    rows = [e.message for e in activity_store.recent(50, source=Source.AUDIT)]
    deletes = [m for m in rows if m.startswith("sidecar.delete ")]
    assert len(deletes) == 2
    assert any("rel-a" in m for m in deletes) and any("rel-b" in m for m in deletes)
    assert any(m.startswith("sidecar.delete_all") and "removed=2" in m for m in rows)


def test_erase_sidecars_counts_and_reports_ones_it_could_not_delete(cfg, caplog):
    """The "Erased N sidecar(s)" count must not over-report. A sidecar that fails to
    unlink was skipped silently and left out of the count, so the user believed
    the nuke was complete while that album kept its identity (#104)."""
    from harmonist import sidecar as scmod

    a = _make_album(cfg, "Deletable")
    b = _make_album(cfg, "Stubborn")
    scmod.write(a, Sidecar(mb_release_id="rel-a"))
    scmod.write(b, Sidecar(mb_release_id="rel-b"))

    stubborn = scmod.sidecar_path(b)
    real_unlink = Path.unlink

    def selective_unlink(self, *args, **kwargs):
        if self == stubborn:
            raise OSError("permission denied")
        return real_unlink(self, *args, **kwargs)

    with mock.patch.object(Path, "unlink", selective_unlink), caplog.at_level("ERROR"):
        removed = scmod.delete_all(cfg.paths.music_dir)

    assert removed == 1, "the undeletable sidecar must not be counted as erased"
    assert scmod.sidecar_path(b).exists()
    assert any("could not be deleted" in r.message for r in caplog.records)


def test_erase_sidecars_removes_only_sidecars(client, cfg):
    from harmonist import sidecar as scmod

    d = _make_album(cfg, "Erasable")
    scmod.write(d, Sidecar(mb_release_id="rel-x"))
    assert scmod.has_sidecar(d)
    audio = d / "01 Track.m4a"
    assert audio.exists()

    r = client.post("/settings/erase-sidecars")
    assert r.status_code == 200
    assert "erased" in r.text.lower()
    # sidecar gone, audio untouched
    assert not scmod.has_sidecar(d)
    assert audio.exists()


def test_erase_sidecars_clears_bandcampsync_checkpoint(client, cfg):
    """Nuke also forgets the sync checkpoint so the next sync re-pages the
    whole collection. ignores.txt is left alone (not a re-download)."""
    state_file = cfg.paths.music_dir / ".bandcampsync-state.json"
    state_file.write_text('{"last_seen_token": "x"}')
    ignores = cfg.paths.config_dir / "ignores.txt"
    ignores.write_text("123  # keep me\n")

    r = client.post("/settings/erase-sidecars")
    assert r.status_code == 200
    assert not state_file.exists()  # checkpoint cleared
    assert "checkpoint reset" in r.text
    assert ignores.read_text() == "123  # keep me\n"  # ignores untouched


def test_settings_shows_sidecar_count(client, cfg):
    from harmonist import sidecar as scmod

    d = _make_album(cfg, "Counted")
    scmod.write(d, Sidecar(mb_release_id="rel-y"))
    r = client.get("/settings")
    assert "Erase sidecars" in r.text
    assert "Maintenance" in r.text


def test_library_new_ribbon_is_time_based(client, cfg):
    """A 'New' ribbon shows on albums downloaded within the last week, not older ones."""
    from datetime import timedelta

    now = datetime.now(UTC)
    fresh = _make_tagged_album(cfg, "FreshDrop", mbid="rel-fresh", tagged_at=now)
    sc.write(
        fresh,
        Sidecar(
            mb_release_id="rel-fresh",
            tagged_at=now,
            downloaded_at=now,
        ),
    )
    stale = _make_tagged_album(cfg, "OldDrop", mbid="rel-stale", tagged_at=now)
    sc.write(
        stale,
        Sidecar(
            mb_release_id="rel-stale",
            tagged_at=now,
            downloaded_at=now - timedelta(days=30),
        ),
    )
    r = client.get("/library")
    assert r.text.count(">New</span>") == 1  # only the fresh download


def test_library_compare_renders_the_tracklist(client, cfg, monkeypatch):
    """The on-demand comparison fetches MB and renders the per-track table."""
    d = _make_tagged_album(cfg, "Verifyme", mbid="rel-verify", tagged_at=datetime.now(UTC))

    def fake_release(mbid):
        return {
            "id": mbid,
            "title": "Verifyme",
            "medium-list": [
                {
                    "position": "1",
                    "track-list": [{"id": "t1", "title": "Track 1", "length": "1000"}],
                }
            ],
        }

    monkeypatch.setattr("harmonist.web.main.mb_lookup.fetch_release", fake_release)
    r = client.get(f"/library/{_id_for(cfg, d)}/compare")
    assert r.status_code == 200
    assert "track-diff" in r.text
    assert "Track 1" in r.text  # MusicBrainz's title for the one track


def test_library_compare_escapes_mb_error_text(client, cfg, monkeypatch):
    """#142: the compare panel's fetch-failure fragment escapes the MB message."""
    d = _make_tagged_album(cfg, "Hostile", mbid="rel-hostile", tagged_at=datetime.now(UTC))

    def boom(mbid):
        raise mb_lookup.MBError("MB ResponseError: <img src=x onerror=alert(1)>")

    monkeypatch.setattr("harmonist.web.main.mb_lookup.fetch_release", boom)

    r = client.get(f"/library/{_id_for(cfg, d)}/compare")
    assert r.status_code == 200
    assert "<img" not in r.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in r.text
    assert "Couldn't fetch from MusicBrainz" in r.text


# ---------- the per-field tag comparison on the album page (#106) ----------


def _release_with_metadata(mbid="rel-cmp"):
    """A release carrying the album-level fields the panel shows."""
    return {
        "id": mbid,
        "title": "Obreel",
        "date": "2019-03-15",
        "artist-credit": [{"artist": {"id": "a1", "name": "Galán, Spieth & Guentner"}}],
        "release-group": {"id": "rg-1", "primary-type": "Album"},
        "label-info-list": [{"label": {"name": "Dial Records"}, "catalog-number": "DIAL 042"}],
        "medium-list": [
            {
                "position": "1",
                "format": "Digital Media",
                "track-list": [{"id": "t1", "title": "Kaskade", "length": "1000"}],
            }
        ],
    }


def test_album_page_compares_tags_field_by_field(client, cfg, monkeypatch):
    """#106: the album panel shows each field against what tagging would write.
    Absent-on-disk fields render as MusicBrainz values, not as conflicts."""
    d = _make_tagged_album(cfg, "Obreel", mbid="rel-cmp", tagged_at=datetime.now(UTC))
    monkeypatch.setattr(
        "harmonist.web.main.mb_lookup.fetch_release", lambda mbid: _release_with_metadata(mbid)
    )

    body = client.get(f"/library/{_id_for(cfg, d)}/compare").text

    assert "fields differ" in body  # the hexagon note's count
    assert "Dial Records" in body and "DIAL 042" in body
    # A MusicBrainz-only value says so without relying on its colour — someone
    # who can't distinguish the purple still gets the mark and its tooltip.
    assert "Not in your files" in body
    for label in ("Album", "Album artist", "Date", "Label", "Cat. no.", "Comment"):
        assert f"<dt>{label}</dt>" in body, f"missing field row: {label}"


def test_a_small_difference_is_marked_inside_the_value(client, cfg, monkeypatch):
    """The reason in-value emphasis exists: a bare year against a full date
    differs by six characters that are invisible without marking."""
    d = _make_tagged_album(cfg, "Obreel", mbid="rel-cmp", tagged_at=datetime.now(UTC))
    from mutagen.mp4 import MP4

    audio = MP4(min(d.glob("*.m4a")))
    audio["\xa9day"] = ["2019"]  # MB says 2019-03-15
    audio.save()
    monkeypatch.setattr(
        "harmonist.web.main.mb_lookup.fetch_release", lambda mbid: _release_with_metadata(mbid)
    )

    body = client.get(f"/library/{_id_for(cfg, d)}/compare").text
    assert '<em class="diff-run">-03-15</em>' in body


def test_tracks_disagreeing_about_a_field_are_counted_not_hidden(client, cfg, monkeypatch):
    """An album tagged unevenly still gets a comparison — annotated with how
    many tracks back the value shown, and the outliers named on hover."""
    import shutil

    from mutagen.mp4 import MP4

    d = _make_tagged_album(cfg, "Uneven", mbid="rel-cmp", tagged_at=datetime.now(UTC))
    first = d / "01 Track.m4a"
    for n in (2, 3):
        shutil.copy(first, d / f"0{n} Track.m4a")
    # Two tracks agree with MusicBrainz, the third doesn't.
    for f in (first, d / "02 Track.m4a"):
        audio = MP4(f)
        audio["aART"] = ["Galán, Spieth & Guentner"]
        audio.save()
    odd = d / "03 Track.m4a"
    audio = MP4(odd)
    audio["aART"] = ["Someone Else"]
    audio.save()
    monkeypatch.setattr(
        "harmonist.web.main.mb_lookup.fetch_release", lambda mbid: _release_with_metadata(mbid)
    )

    body = client.get(f"/library/{_id_for(cfg, d)}/compare").text
    assert "2 of 3" in body
    assert odd.name in body  # the outlier is named, for the hover


def test_the_comparison_is_absent_from_the_library_modal(client, cfg, monkeypatch):
    """The modal answers "is this the right release?" and links through for the
    rest (#103). It loads the same fragment, so the tag comparison must be gated
    on the page's context rather than leaking into the dialog."""
    d = _make_tagged_album(cfg, "Modal", mbid="rel-cmp", tagged_at=datetime.now(UTC))
    monkeypatch.setattr(
        "harmonist.web.main.mb_lookup.fetch_release", lambda mbid: _release_with_metadata(mbid)
    )
    body = client.get(f"/album/{_id_for(cfg, d)}").text
    assert "<dt>Cat. no.</dt>" not in body


def test_library_compare_flags_title_discrepancy(client, cfg, monkeypatch):
    """When on-disk track titles differ from MB (e.g. a featured-artist tidy-up
    on MB after tagging), the tracklist reports the metadata difference even
    though the lengths line up perfectly (issue #29)."""
    d = _make_tagged_album(cfg, "Mismatch", mbid="rel-mm", tagged_at=datetime.now(UTC))
    audio = MP4(d / "01 Track.m4a")
    audio[ATOM_TITLE] = ["Ground Glass [w/ Foxes in Fiction]"]
    audio.save()

    def fake_release(mbid):
        return {
            "id": mbid,
            "title": "Mismatch",
            "medium-list": [
                {
                    "position": "1",
                    # MB has the cleaned-up title; the file still carries the old one.
                    "track-list": [{"id": "t1", "title": "Ground Glass", "length": "1000"}],
                }
            ],
        }

    monkeypatch.setattr("harmonist.web.main.mb_lookup.fetch_release", fake_release)
    r = client.get(f"/library/{_id_for(cfg, d)}/compare")
    assert r.status_code == 200
    # Both titles on the page: the file's, and MusicBrainz's beneath it. The
    # file's arrives split across diff runs — the featured-artist suffix is
    # marked in place — so the assertion is on the marked run itself.
    assert '<em class="diff-run"> [w/ Foxes in Fiction]</em>' in r.text
    assert "1 of 1 tracks differs from MusicBrainz" in r.text
    assert "match MusicBrainz" not in r.text


def test_album_page_loads_the_comparison_lazily(client, cfg):
    """The MusicBrainz comparison is fetched on load rather than rendered with
    the page: it costs an MB request, and the budget is one per second
    (review-gate item 6). Lazy keeps that cost to opening the page."""
    d = _make_tagged_album(cfg, "HasVerify", mbid="rel-v2", tagged_at=datetime.now(UTC))
    aid = _id_for(cfg, d)

    page = client.get(f"/album/{aid}").text
    assert f"/library/{aid}/compare" in page
    assert 'hx-trigger="load"' in page


def test_library_detail_shows_ambiguous_bandcamp_ids(client, cfg):
    """An ambiguously-linked album (COMPLETE, candidate ids but no single id)
    shows the candidate item ids in the store badge tooltip."""
    d = _make_album(cfg, "Ambi")
    audio = MP4(d / "01 Track.m4a")
    audio[ATOM_MB_ALBUM_ID] = [b"rel-ambi"]
    audio.save()
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/ambi",
            bandcamp=BandcampInfo(item_id=None, candidate_item_ids=[111, 222]),
            mb_release_id="rel-ambi",
            tagged_at=datetime.now(UTC),
        ),
    )
    r = client.get(f"/album/{_id_for(cfg, d)}")
    assert "ambiguous: item #111 or #222" in r.text


def test_about_page_renders(client):
    r = client.get("/about")
    assert r.status_code == 200
    assert "About Harmonist" in r.text
    assert "GPL-3.0-or-later" in r.text
    assert "mutagen" in r.text
    assert "MusicBrainz" in r.text
    assert "github.com/randomphrase/harmonist" in r.text


def test_git_sha_prefers_baked_env(monkeypatch):
    """The build SHA is taken from HARMONIST_GIT_SHA (baked at Docker build) when
    set — so the container reports its commit even with no .git present."""
    from harmonist.web.main import _git_sha

    monkeypatch.setenv("HARMONIST_GIT_SHA", "deadbeefcafebabe1234")
    assert _git_sha() == "deadbeefcafe"  # truncated to 12


def test_container_detected_from_env_selects_no_timestamp_format(monkeypatch):
    """HARMONIST_IN_CONTAINER=1 marks a container → log format omits the timestamp
    (the driver stamps at capture); the dev format keeps one."""
    from harmonist.web import main as m

    monkeypatch.setenv("HARMONIST_IN_CONTAINER", "1")
    assert m._in_container() is True
    # The two formats differ exactly by the timestamp field.
    assert "asctime" not in m._LOG_FORMAT_NO_TS
    assert "asctime" in m._LOG_FORMAT_WITH_TS


# ---------- startup permission gate ----------


def test_validate_runtime_paths_ok_and_creates_dirs(cfg):
    from harmonist.web.main import _validate_runtime_paths

    _validate_runtime_paths(cfg)  # must not raise
    assert cfg.paths.music_dir.is_dir()
    assert cfg.paths.config_dir.is_dir()
    assert not list(cfg.paths.music_dir.glob(".harmonist-write-test-*"))  # probe cleaned up


def test_validate_runtime_paths_raises_when_unwritable(tmp_path):
    from harmonist.web.main import _validate_runtime_paths

    # A file where the music dir should be → mkdir/touch fails → fail fast.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    bad = Config(
        paths=PathsConfig(config_dir=tmp_path / "config", music_dir=blocker / "music"),
        bandcamp=BandcampConfig(),
        server=ServerConfig(),
        test=TestConfig(mode="fixture"),
    )
    with pytest.raises(RuntimeError, match="not writable"):
        _validate_runtime_paths(bad)


def test_run_bandcamp_sync_precreates_ignores_file(cfg, monkeypatch):
    """bandcampsync seeds a missing ignores file from a template that only
    exists in its own Docker image (/ignores.template.txt). We pre-create the
    file so its copy is skipped — no 'No such file' on first sync."""
    from harmonist.web import main as main_mod

    cfg.paths.config_dir.mkdir(parents=True, exist_ok=True)
    cfg.cookies_file.write_text("cookie", encoding="utf-8")  # get past the cookies check
    assert not cfg.ignores_file.exists()

    called: dict[str, bool] = {}
    monkeypatch.setattr(main_mod, "HarmonistSyncer", lambda *a, **k: called.setdefault("yes", True))

    main_mod._run_bandcamp_sync(cfg)

    assert cfg.ignores_file.exists()  # pre-created → bandcampsync won't copy the template
    # Seeded from the vendored template, not left blank.
    assert "exclude releases from downloads" in cfg.ignores_file.read_text()
    assert called.get("yes")


def test_run_bandcamp_sync_keeps_existing_ignores_file(cfg, monkeypatch):
    """An existing ignores file (with the user's ids) is never overwritten."""
    from harmonist.web import main as main_mod

    cfg.paths.config_dir.mkdir(parents=True, exist_ok=True)
    cfg.cookies_file.write_text("cookie", encoding="utf-8")
    cfg.ignores_file.write_text("12345  # keep me\n", encoding="utf-8")
    monkeypatch.setattr(main_mod, "HarmonistSyncer", lambda *a, **k: None)

    main_mod._run_bandcamp_sync(cfg)

    assert cfg.ignores_file.read_text() == "12345  # keep me\n"


# ---------- Potential-download actions ----------


def _pp(item_id, *, band="B", title="T", url="https://x.bandcamp.com/album/y", fmt="alac"):
    from harmonist.pending_downloads import PendingPurchase

    return PendingPurchase(item_id=item_id, band=band, title=title, url=url, fmt=fmt)


def test_pending_skip_ignores_and_removes(client, cfg):
    from harmonist import pending_downloads as pd

    pd.replace_all([_pp(42)])
    r = client.post("/pending/42/skip")
    assert r.status_code == 200
    assert pd.get(42) is None  # dropped from the store
    assert "42" in cfg.ignores_file.read_text()  # appended to ignores.txt


def test_pending_download_approves_and_removes(client):
    from harmonist import pending_downloads as pd

    pd.replace_all([_pp(43)])
    r = client.post("/pending/43/download")
    assert r.status_code == 200
    assert pd.get(43) is None
    assert pd.is_approved(43)  # the next sync will fetch it


def test_pending_card_has_inline_library_search(client):
    """The card carries the unified 'already in your library?' live search inline
    (no separate Match toggle) — the search box + results container are always
    present, so the auto-match and a manual search share one coherent surface."""
    from harmonist import pending_downloads as pd

    pd.replace_all([_pp(44, band="Variant", title="Sequential Sleep")])
    body = client.get("/tasks").text
    assert "Already in your library?" in body
    assert 'name="q"' in body  # the inline library search input
    assert 'id="match-results-44"' in body  # the results container the search targets


def test_pending_match_results_endpoint_searches_library(client, cfg):
    """The inline search hits /pending/{id}/match/results, returning library rows."""
    from harmonist import pending_downloads as pd

    _make_album(cfg, "Sequential Sleep")
    pd.replace_all([_pp(44, band="Variant", title="Sequential Sleep")])
    r = client.get("/pending/44/match/results", params={"q": "Sequential"})
    assert r.status_code == 200
    assert "Sequential Sleep" in r.text
    assert "/pending/44/match" in r.text  # each row Links via the match POST


def test_pending_result_opens_the_album_page_to_verify(client, cfg):
    """#129: verifying a match opens the album's page in a NEW TAB rather than a
    dialog. You're mid-task in the pending list, and navigating away from it is
    the one thing the dialog was protecting against — a new tab protects it just
    as well, without a dialog lifecycle, and the Link button is still right
    there in the row."""
    from harmonist import pending_downloads as pd
    from harmonist import scanner
    from harmonist import sidecar as scmod

    d = _make_album(cfg, "Black Soma", mbid="rel-bs")
    scmod.write(d, Sidecar(mb_release_id="rel-bs"))
    album_id = _id_for(cfg, d)
    a = next(x for x in scanner.scan(cfg.paths.music_dir) if x.path == d)
    # Pending band/title match the on-disk album → it's seeded as the suggestion,
    # whose row deep-links into the verify modal.
    pd.replace_all([_pp(77, band=a.artist, title=a.title, url="https://3six.net/album/black-soma")])

    body = client.get("/tasks").text
    # The result row links to the album's page, in a new tab, with noopener —
    # target=_blank without it hands the opened page a reference back.
    assert f'href="/album/{album_id}" target="_blank" rel="noopener"' in body
    # …and Link is still on the row itself, carrying the pending item and album.
    assert "/pending/77/match" in body
    assert f'"album_id": "{album_id}"' in body


def test_pending_match_link_fills_item_id(client, cfg):
    """Linking a potential download to an on-disk album writes the purchase's
    item_id + store_url onto that album's sidecar, and drops it from pending."""
    from harmonist import pending_downloads as pd
    from harmonist import sidecar as scmod

    d = _make_album(cfg, "Existing", mbid="rel-xyz")
    scmod.write(d, Sidecar(mb_release_id="rel-xyz"))
    album_id = _id_for(cfg, d)

    pd.replace_all([_pp(45, url="https://x.bandcamp.com/album/y")])
    from harmonist import activity

    activity.clear()
    r = client.post("/pending/45/match", data={"album_id": album_id})
    assert r.status_code == 200
    assert pd.get(45) is None
    sc = scmod.read(d)
    assert sc.bandcamp is not None
    assert sc.bandcamp.item_id == 45
    assert sc.store_url == "https://x.bandcamp.com/album/y"
    # A manual link is logged like an auto-link: "Linked <purchase> → <album>".
    assert any(m.message.startswith("Linked ") for m in activity.recent(5))


def test_pending_match_to_library_album_skips_rescan(client, cfg, monkeypatch):
    """Matching a potential download to a Library (COMPLETE) album leaves it
    complete — no inbox change — so it must NOT trigger a full rescan (the flicker
    the user hit while working through the adoption residue)."""
    from datetime import datetime

    from harmonist import pending_downloads as pd

    d = _make_tagged_album(cfg, "Lib Match", mbid="rel-lm", tagged_at=datetime.now(UTC))
    calls: list[int] = []
    monkeypatch.setattr(
        client.app.state.scan_runner, "request_scan", lambda *a, **k: calls.append(1)
    )
    pd.replace_all([_pp(46, url="https://x.bandcamp.com/album/z")])
    r = client.post("/pending/46/match", data={"album_id": _id_for(cfg, d)})
    assert r.status_code == 200
    assert calls == []  # no rescan → no inbox flicker


def test_pending_match_to_surrender_triggers_rescan(client, cfg, monkeypatch):
    """Matching a Needs-Sync (surrender) album moves it out of the inbox → a
    rescan IS needed so its card clears."""
    from harmonist import pending_downloads as pd

    d = _needs_sync_album(cfg, "Surr Match", "rel-sm")
    calls: list[int] = []
    monkeypatch.setattr(
        client.app.state.scan_runner, "request_scan", lambda *a, **k: calls.append(1)
    )
    pd.replace_all([_pp(47, url="https://x.bandcamp.com/album/w")])
    r = client.post("/pending/47/match", data={"album_id": _id_for(cfg, d)})
    assert r.status_code == 200
    assert calls == [1]  # rescanned


def test_claim_pending_by_store_url_is_subdomain_insensitive(client):
    """Tagging an album whose store_url matches a pending purchase claims it out of
    the list (the purchase is now on disk) — matched by slug, so a label vs artist
    subdomain for the same edition still resolves. Unrelated pending is untouched.
    This is the belt-and-braces behind confirming a mis-tag."""
    from harmonist import pending_downloads as pd
    from harmonist.web.main import _claim_pending_by_store_url

    pd.replace_all(
        [
            _pp(60, url="https://label.bandcamp.com/album/x"),
            _pp(61, url="https://other.bandcamp.com/album/z"),
        ]
    )
    # Same slug, DIFFERENT subdomain (artist page vs label page).
    _claim_pending_by_store_url("https://artist.bandcamp.com/album/x")
    assert pd.get(60) is None
    assert pd.get(61) is not None
    # No-op on empty / non-matching input.
    _claim_pending_by_store_url(None)
    _claim_pending_by_store_url("https://nope.bandcamp.com/album/nomatch")
    assert pd.get(61) is not None


def test_reconcile_suggestions_unambiguous_both_directions(cfg):
    """Case-B pairing by approximate artist+title (store_url join failed): the
    potential download points at the on-disk album, and the surrender album points
    back at the potential download — both unambiguous."""
    from harmonist import scanner
    from harmonist.web.main import _reconcile_suggestions

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    d = _needs_sync_album(cfg, "Reunion", "rel-r")
    albums = scanner.scan(cfg.paths.music_dir)
    a = next(x for x in albums if x.path == d)
    # Same name, DIFFERENT slug (the realistic case B — a re-slug / rip).
    pend = [_pp(70, band=a.artist, title=a.title, url="https://x.bandcamp.com/album/reunited")]
    ps, ss = _reconcile_suggestions(albums, pend, cfg.paths.music_dir)
    assert ps[70]["id"] == a.id  # P→A
    assert ss[a.id].item_id == 70  # A→P


def test_reconcile_suggestions_ambiguous_is_skipped(cfg):
    """Two potential downloads with the same normalized name → the album can't tell
    which it is → no surrender suggestion (safety via uniqueness). Each pending
    still uniquely points at the one album."""
    from harmonist import scanner
    from harmonist.web.main import _reconcile_suggestions

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    d = _needs_sync_album(cfg, "Reunion", "rel-r")
    albums = scanner.scan(cfg.paths.music_dir)
    a = next(x for x in albums if x.path == d)
    pend = [
        _pp(70, band=a.artist, title=a.title, url="https://x.bandcamp.com/album/a"),
        _pp(71, band=a.artist, title=a.title, url="https://y.bandcamp.com/album/b"),
    ]
    ps, ss = _reconcile_suggestions(albums, pend, cfg.paths.music_dir)
    assert a.id not in ss  # ambiguous on the album side
    assert ps[70]["id"] == a.id  # each pending still uniquely points at the album
    assert ps[71]["id"] == a.id


def test_reconcile_suggestions_skips_already_linked_album(cfg):
    """An album already linked to a purchase (item_id set) is not a case-B
    candidate — it's done, so no suggestion points at it."""
    from harmonist import scanner
    from harmonist.web.main import _reconcile_suggestions

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    d = _make_album(cfg, "Linked")
    sc.write(
        d,
        Sidecar(
            mb_release_id="rel-l",
            store_url="https://x.bandcamp.com/album/linked",
            bandcamp=BandcampInfo(item_id=123),
        ),
    )
    albums = scanner.scan(cfg.paths.music_dir)
    a = next(x for x in albums if x.path == d)
    pend = [_pp(80, band=a.artist, title=a.title, url="https://x.bandcamp.com/album/other")]
    ps, _ = _reconcile_suggestions(albums, pend, cfg.paths.music_dir)
    assert 80 not in ps


def test_reconcile_suggestions_includes_library_albums(cfg):
    """A COMPLETE (Library) album that's unlinked (no item_id) is still a match
    candidate — the "36" case: a real Bandcamp purchase whose files lost the
    Bandcamp comment, so it landed in Library. The potential-download match must
    reach into the library, not just the inbox, to recover it."""
    from harmonist import scanner
    from harmonist.models import AlbumState
    from harmonist.web.main import _reconcile_suggestions

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    # Tagged (files carry the MBID), no Bandcamp store_url, no item_id → COMPLETE.
    d = _make_album(cfg, "Black Soma", mbid="rel-bs")
    sc.write(d, Sidecar(mb_release_id="rel-bs"))
    albums = scanner.scan(cfg.paths.music_dir)
    a = next(x for x in albums if x.path == d)
    assert a.state == AlbumState.COMPLETE  # it's in the Library, not the inbox
    pend = [_pp(81, band=a.artist, title=a.title, url="https://3six.bandcamp.com/album/black-soma")]
    ps, _ = _reconcile_suggestions(albums, pend, cfg.paths.music_dir)
    assert ps[81]["id"] == a.id  # the library album is offered as the match


def _surrendered_album(cfg, name: str, mbid: str) -> Path:
    """An album that surrendered: files tagged with `mbid`, but the sidecar's
    mb_release_id was cleared and an `unmatched_purchase` candidate stashed →
    NEEDS_MBID (the 'No Bandcamp purchase found' card)."""
    d = _make_album(cfg, name, mbid=mbid)
    sc.write(
        d,
        Sidecar(
            store_url=f"https://x.bandcamp.com/album/{name.lower().replace(' ', '-')}",
            mb_release_id=None,
            mb_match_candidate=MatchCandidate(
                mb_release_id=mbid,
                confidence="exact",
                file_count=1,
                track_count=1,
                unmatched_purchase=True,
            ),
        ),
    )
    return d


def test_reconcile_suggestions_includes_surrendered_albums(cfg):
    """A surrendered album (NEEDS_MBID + unmatched_purchase) gets the reverse
    suggestion too, not just NEEDS_SYNC — that's where unmatched albums live."""
    from harmonist import scanner
    from harmonist.models import AlbumState
    from harmonist.web.main import _reconcile_suggestions

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    d = _surrendered_album(cfg, "Surrendered One", "rel-s1")
    albums = scanner.scan(cfg.paths.music_dir)
    a = next(x for x in albums if x.path == d)
    assert a.state == AlbumState.NEEDS_MBID
    pend = [_pp(60, band=a.artist, title=a.title, url="https://x.bandcamp.com/album/other")]
    _, ss = _reconcile_suggestions(albums, pend, cfg.paths.music_dir)
    assert ss[a.id].item_id == 60


def test_norm_artist_collapses_collab_and_feat_variants():
    """A collaboration written differently across sources normalizes the same:
    Bandcamp 'A / B' vs tagged 'A and B'/'A & B', and 'A feat. B' vs 'A'."""
    from harmonist.web.main import _norm_artist

    assert _norm_artist("Jah Wobble and Bill Laswell") == _norm_artist("Jah Wobble / Bill Laswell")
    assert _norm_artist("A & B") == _norm_artist("A / B")
    assert _norm_artist("Bonobo feat. Andreya Triana") == _norm_artist("Bonobo")
    assert _norm_artist("Above and Beyond") != _norm_artist("Tycho")  # not everything collapses


def test_reconcile_suggestions_matches_collab_artist_separator(cfg):
    """The Radioaxiom case: on-disk artist 'A and B' (tags) vs purchase band 'A / B'
    (Bandcamp) still matches — the artist normalization collapses the separator."""
    from harmonist import scanner
    from harmonist.web.main import _reconcile_suggestions

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    d = _make_album(cfg, "Radioaxiom", mbid="rel-ra")
    f = next(d.glob("*.m4a"))
    audio = MP4(f)
    audio[ATOM_ARTIST] = ["Jah Wobble and Bill Laswell"]
    audio[ATOM_ALBUM] = ["Radioaxiom"]
    audio.save()
    sc.write(
        d,
        Sidecar(
            store_url="http://billlaswell.bandcamp.com",
            mb_release_id=None,
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-ra",
                confidence="exact",
                file_count=1,
                track_count=1,
                unmatched_purchase=True,
            ),
        ),
    )
    albums = scanner.scan(cfg.paths.music_dir)
    a = next(x for x in albums if x.path == d)
    assert a.artist == "Jah Wobble and Bill Laswell"
    pend = [
        _pp(70, band="Jah Wobble / Bill Laswell", title="Radioaxiom", url="https://x.net/album/ra")
    ]
    ps, ss = _reconcile_suggestions(albums, pend, cfg.paths.music_dir)
    assert 70 in ps  # forward: pending → album
    assert a.id in ss  # reverse: surrendered album → pending


def test_link_surrendered_album_un_surrenders_to_complete(client, cfg):
    """Linking a potential download to a surrendered album restores its release id
    from the candidate (files are still tagged) → Library, and links the item_id."""
    from harmonist import pending_downloads as pd
    from harmonist import sidecar as scmod
    from harmonist.models import AlbumState
    from harmonist.scanner import scan

    d = _surrendered_album(cfg, "Undo Surrender", "rel-us")
    pd.replace_all([_pp(61, url="https://x.bandcamp.com/album/undo")])
    r = client.post("/pending/61/match", data={"album_id": _id_for(cfg, d)})
    assert r.status_code == 200
    loaded = scmod.read(d)
    assert loaded.bandcamp.item_id == 61
    assert loaded.mb_release_id == "rel-us"  # restored from the candidate
    assert loaded.mb_match_candidate is None  # surrender cleared
    assert next(x for x in scan(cfg.paths.music_dir) if x.path == d).state == AlbumState.COMPLETE


def test_surrender_card_shows_reverse_suggestion(client, cfg):
    """A surrendered album with a matching potential download LEADS with the Link
    (positive framing), not the contradictory 'No Bandcamp purchase found' panel."""
    from harmonist import pending_downloads as pd
    from harmonist import scanner

    d = _surrendered_album(cfg, "Reverse Vis", "rel-rv")
    a = next(x for x in scanner.scan(cfg.paths.music_dir) if x.path == d)
    pd.replace_all([_pp(62, band=a.artist, title=a.title, url="https://x.bandcamp.com/album/rv2")])
    body = client.get("/tasks").text
    assert "Looks like a purchase you own" in body
    assert "/pending/62/match" in body
    # With a match, the contradictory "No Bandcamp purchase found" panel is dropped.
    card = body[body.index('id="task-' + a.id) :]
    assert "No Bandcamp purchase found" not in card[: card.index("</section>")]


def test_case_b_suggestions_render_on_both_cards(client, cfg):
    """The potential-download card shows 'Already in your library?' with a Link-these
    button to the matched album, and the surrender card shows the reverse — both
    posting to the same /pending/{id}/match link."""
    from harmonist import pending_downloads as pd
    from harmonist import scanner

    d = _needs_sync_album(cfg, "Reunion", "rel-r")
    a = next(x for x in scanner.scan(cfg.paths.music_dir) if x.path == d)
    pd.replace_all(
        [_pp(90, band=a.artist, title=a.title, url="https://x.bandcamp.com/album/reunited")]
    )
    body = client.get("/tasks").text
    assert "Already in your library?" in body  # potential-download card suggestion
    assert "You may have already downloaded this purchase:" in body  # surrender card
    assert f'"album_id": "{a.id}"' in body  # both Links target this album
    assert "/pending/90/match" in body


def test_pending_section_open_by_default_with_summary(client):
    """The potential-downloads list is an OPEN <details> with a one-line summary —
    these purchases need reviewing, so we don't hide them behind an expander. The
    user can still collapse it (open state is preserved across swaps)."""
    from harmonist import pending_downloads as pd

    pd.replace_all([_pp(50), _pp(51)])
    body = client.get("/tasks").text
    assert 'id="group-PENDING"' in body
    # Open by default.
    assert '<details id="group-PENDING" open' in body
    assert "you own but" in body  # the summary line
    assert "· 2" in body  # the count


# ---------- Sync popover (link-only override + max-downloads) ----------


def test_sync_popover_forces_link_only_and_persists_cap(client, monkeypatch):
    """The popover posts from_popover + link_only + max_downloads → the runner gets
    an explicit link-only override, and the per-sync cap persists to config."""
    runner = client.app.state.sync_runner
    monkeypatch.setattr(runner, "start", lambda: None)  # don't spawn a real sync
    r = client.post(
        "/sync", data={"from_popover": "true", "link_only": "true", "max_downloads": "0"}
    )
    assert r.status_code == 200
    assert runner.link_only_override is True
    assert client.app.state.cfg.bandcamp.max_downloads_per_sync == 0


def test_sync_popover_unchecked_forces_download_mode(client, monkeypatch):
    """from_popover with the link-only checkbox absent → explicit False (not auto)."""
    runner = client.app.state.sync_runner
    monkeypatch.setattr(runner, "start", lambda: None)
    r = client.post("/sync", data={"from_popover": "true", "max_downloads": "5"})
    assert r.status_code == 200
    assert runner.link_only_override is False


def test_plain_sync_button_leaves_link_only_auto(client, monkeypatch):
    """The plain Sync button (no popover fields) → None → auto-detect link-only."""
    runner = client.app.state.sync_runner
    monkeypatch.setattr(runner, "start", lambda: None)
    r = client.post("/sync")
    assert r.status_code == 200
    assert runner.link_only_override is None


def test_sync_popover_exposes_ids_for_live_default_js(client, cfg):
    """The popover renders the ids the /status poll binds to — the JS derives the
    link-only default from the live count and greys max-downloads. Guards the
    contract so a template rename can't silently break the wiring."""
    body = "# Netscape HTTP Cookie File\n.bandcamp.com\tTRUE\t/\tFALSE\t0\tident\tabc\n"
    client.post("/bandcamp/cookies", data={"cookies_text": body})
    home = client.get("/").text
    assert 'id="sync-control"' in home
    assert 'id="sync-link-only"' in home
    assert 'id="sync-max-row"' in home
    assert 'id="sync-max-downloads"' in home


# ---------- ignores.txt: write region + the "Won't download" surface (#19/#77) ----------

_IGNORES_WITH_SEPARATOR = """\
# This file allows you to exclude releases from downloads.
1111  # Manually / Added
# =========================================================
2222  # Already / Downloaded
3333  # Also Already / Downloaded
"""


def test_ignore_is_written_above_the_separator(client, cfg):
    """#77: a declined purchase belongs in the USER region. Appending put it in
    bandcampsync's auto-managed region, where it's indistinguishable from
    'already downloaded' and gets clobbered when bandcampsync rewrites the file."""
    from harmonist.web.main import _append_ignore

    cfg.ignores_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.ignores_file.write_text(_IGNORES_WITH_SEPARATOR)

    _append_ignore(cfg.ignores_file, 4444, "New / Ignore")

    lines = cfg.ignores_file.read_text().splitlines()
    sep = next(i for i, ln in enumerate(lines) if "=========" in ln)
    ours = next(i for i, ln in enumerate(lines) if ln.startswith("4444"))
    assert ours < sep, "user ignore must land above the separator"
    # The auto-managed region is untouched.
    assert lines[sep + 1 :] == ["2222  # Already / Downloaded", "3333  # Also Already / Downloaded"]


def test_ignore_with_no_separator_still_appends(client, cfg):
    """A fresh ignores.txt has no separator yet (bandcampsync adds it at the end
    on first run), so everything present is user data — append is correct."""
    from harmonist.web.main import _append_ignore

    cfg.ignores_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.ignores_file.write_text("# header only\n")
    _append_ignore(cfg.ignores_file, 55, "Solo / Ignore")
    assert "55  # Solo / Ignore" in cfg.ignores_file.read_text()


def test_ignored_surface_lists_only_user_entries(client, cfg):
    """#19: the surface must NOT list the auto-managed region — that's every
    album already downloaded, i.e. the user's whole collection."""
    from harmonist.web.main import _read_user_ignores

    cfg.ignores_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.ignores_file.write_text(_IGNORES_WITH_SEPARATOR)

    got = _read_user_ignores(cfg.ignores_file)
    assert [i["item_id"] for i in got] == [1111]
    assert got[0]["label"] == "Manually / Added"

    # Lives on Settings, not the Inbox: a declined purchase is resolved, not
    # pending, so the Inbox stays a pure work queue.
    settings = client.get("/settings").text
    assert "Manually / Added" in settings
    assert "Already / Downloaded" not in settings
    assert "Manually / Added" not in client.get("/tasks").text


def test_restore_removes_only_from_the_user_region(client, cfg):
    """Restoring must never drop an id from the auto-managed region — that would
    make the next sync re-download an album the user already has."""
    from harmonist.web.main import _read_user_ignores

    cfg.ignores_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.ignores_file.write_text(_IGNORES_WITH_SEPARATOR)

    r = client.post("/ignored/1111/restore")
    assert r.status_code == 200
    assert _read_user_ignores(cfg.ignores_file) == []
    # ...and the downloaded ids survive.
    text = cfg.ignores_file.read_text()
    assert "2222" in text and "3333" in text

    # Restoring an id that only exists in the auto region is a no-op.
    client.post("/ignored/2222/restore")
    assert "2222" in cfg.ignores_file.read_text()


def test_demo_mode_sandboxes_the_ignores_file(monkeypatch, tmp_path):
    """#77: demo shares the real CONFIG dir, so 'Don't download' on a demo
    purchase was appending fixture item_ids to the user's genuine ignores.txt."""
    from harmonist import config as config_mod

    monkeypatch.setenv("HARMONIST_DEMO_MODE", "1")
    monkeypatch.setenv("HARMONIST_CONFIG_DIR", str(tmp_path / "config"))
    cfg = config_mod.load()

    assert cfg.demo_mode
    assert cfg.paths.config_dir not in cfg.ignores_file.parents
    assert cfg.ignores_file.parent == cfg.paths.music_dir  # the demo sandbox


def test_ignore_is_not_written_twice(client, cfg):
    """#79: re-deciding the same purchase must not append a duplicate. Ours was
    the only unguarded writer — bandcampsync checks is_ignored() before its own
    adds — which is how a real file grew 55 lines for 31 unique ids."""
    from harmonist.web.main import _append_ignore, _read_user_ignores

    cfg.ignores_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.ignores_file.write_text("# header\n")

    _append_ignore(cfg.ignores_file, 4242, "Band — Album")
    _append_ignore(cfg.ignores_file, 4242, "Band — Album")
    _append_ignore(cfg.ignores_file, 4242, "Band — Album (different label)")

    assert cfg.ignores_file.read_text().count("4242") == 1
    assert len(_read_user_ignores(cfg.ignores_file)) == 1


def test_ignore_already_in_the_auto_region_is_a_noop(client, cfg):
    """An id bandcampsync already recorded is honoured wherever it sits, so
    re-adding it to the user region would change nothing and just inflate the
    file — and would wrongly offer an album you HAVE for un-ignoring."""
    from harmonist.web.main import _append_ignore, _read_user_ignores

    cfg.ignores_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.ignores_file.write_text(_IGNORES_WITH_SEPARATOR)

    _append_ignore(cfg.ignores_file, 2222, "Already / Downloaded")

    assert cfg.ignores_file.read_text().count("2222") == 1
    # Not promoted into the user region either.
    assert [i["item_id"] for i in _read_user_ignores(cfg.ignores_file)] == [1111]


# ---------- action correlation coverage (#84) ----------
#
# The mechanism is unit-tested in test_activity_store.py. The risk here is
# COVERAGE: an audit.record() outside any scope silently gets NULL, so each
# entry point that opens a scope needs one test proving it actually does.


def test_request_scope_correlates_the_action_with_its_audit_records(client, cfg, monkeypatch):
    """A state-changing request opens one scope, so the activity entry the
    handler writes and the sidecar audit beneath it share an action_id — even
    though audit.record() is called from deep inside sidecar.write(), which knows
    nothing about requests. This is what the contextvar buys."""
    from harmonist import activity, activity_store

    # Confirm, not re-tag: _audit_sidecar_change only records when a load-bearing
    # actually moves, so re-tagging to the SAME mbid audits nothing and would
    # make this pass vacuously. Confirming moves temp_uid -> mbid.
    d = _make_album(cfg, "Correlated")
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/corr",
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-corr", confidence="exact", file_count=1, track_count=1
            ),
        ),
    )
    aid = _id_for(cfg, d)
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release", lambda mbid: _release_for_match(mbid, n_tracks=1)
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)
    activity.clear()

    assert client.post(f"/confirm/{aid}").status_code == 200

    entry = next(e for e in activity.recent(10) if "Tagged" in e.message)
    assert entry.action_id, "the request opened no action scope"
    detail = activity_store.audit_by_action([entry.action_id])
    assert detail.get(entry.action_id), "no audit records correlated to the action"


def test_get_requests_open_no_action_scope(client, cfg):
    """GETs mutate nothing, so they shouldn't mint an id — an action id that
    correlates nothing is noise in the column."""
    from harmonist import activity, activity_store

    _make_album(cfg, "Idle")
    activity.clear()
    client.get("/tasks")
    activity.record("written during no action")

    assert activity_store.recent()[0].action_id is None


def test_reconcile_gives_each_album_its_own_action(cfg):
    """Per album, not per run: reconcile writes a sidecar and an activity entry
    for each, so each must be separately revertible."""
    from harmonist import activity
    from harmonist.web.reconcile_runner import reconcile_pending_orphans

    _make_album(cfg, "OrphanOne", mbid="rel-one")
    _make_album(cfg, "OrphanTwo", mbid="rel-two")
    activity.clear()

    reconcile_pending_orphans(cfg.paths.music_dir, fetch_urls=lambda mbid: [])

    ids = {e.action_id for e in activity.recent(20) if "New →" in e.message}
    assert len(ids) == 2, "the two albums should not share one action id"
    assert None not in ids


# ---------- A broken history store must not render as an empty one (#104) ----------


@pytest.fixture
def broken_store(monkeypatch):
    """Every activity_store operation fails, as a locked or corrupt DB would."""
    import sqlite3

    from harmonist import activity_store

    def boom():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(activity_store, "_ensure", boom)


def test_activity_feed_says_unavailable_rather_than_empty(client, broken_store):
    """The feed is re-polled every 2s, so rendering a store failure as "No
    activity yet" is how a broken store goes unnoticed for weeks on a NAS."""
    r = client.get("/activity")
    assert r.status_code == 200  # a polling panel must not 500 on a loop
    assert "Activity unavailable" in r.text
    assert "No activity yet" not in r.text


def test_album_page_says_history_unavailable_rather_than_nothing_recorded(client, cfg):
    """The sharpest form of the lie: the page would state as fact that Harmonist
    has never touched the album, at the moment the user asks what it did."""
    d = _make_album(cfg, "BrokenHistory")
    sc.write(d, sc.Sidecar(store_url="https://x.bandcamp.com/album/y"))
    album_id = sc.album_id_for(d)
    assert album_id

    # Control first: with a working store the page renders real history (the
    # sidecar write above audited itself) and no unavailable notice.
    ok = client.get(f"/album/{album_id}")
    assert ok.status_code == 200
    assert "sidecar.create" in ok.text
    assert "History unavailable" not in ok.text

    from harmonist import activity_store

    def boom(*a, **k):
        raise activity_store.StoreUnavailableError("nope")

    with mock.patch.object(activity_store, "album_history", boom):
        r = client.get(f"/album/{album_id}")
    assert r.status_code == 200  # tracklist and actions don't need the store
    assert "History unavailable" in r.text
    assert "Nothing recorded for this album yet" not in r.text
    assert "sidecar.create" not in r.text  # no half-history presented as whole


def test_album_page_history_offers_show_details_checked_by_default(client, cfg):
    """Checked by default, unlike the feed: a single album's history is often
    mostly audit rows, and an adopted album may have nothing else at all — so
    defaulting it off would show an empty history for exactly the albums this
    page exists to explain (#123)."""
    d = _make_album(cfg, "HistoryToggle")
    sc.write(d, sc.Sidecar(store_url="https://x.bandcamp.com/album/y"))
    album_id = sc.album_id_for(d)

    body = client.get(f"/album/{album_id}").text
    assert 'type="checkbox" class="cursor-pointer" checked' in body
    assert "Show details" in body
    # Audit rows are marked for the CSS toggle, and both counts are rendered so
    # the heading can follow it without JS.
    assert "album-history__audit" in body
    assert "album-history__count-full" in body
    assert "album-history__count-brief" in body


def test_unresolvable_link_is_503_not_404_when_the_store_is_broken(client, cfg, broken_store):
    """resolve_alias returning None means "never superseded", which sends
    _find_album to a 404. One DB hiccup must not tell the user an album that is
    sitting on disk does not exist."""
    r = client.get("/album/some-id-that-is-not-in-the-snapshot")
    assert r.status_code == 503
    assert "history store is unavailable" in r.text


def test_a_genuine_miss_is_still_404(client, cfg):
    """Control for the test above — a working store must still 404 an id that
    really isn't there, or the 503 would just be masking every miss."""
    r = client.get("/album/some-id-that-is-not-in-the-snapshot")
    assert r.status_code == 404
