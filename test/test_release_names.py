"""A single release is named by its title and disambiguation (#635).

Sibling releases share a title, so "MusicBrainz ↗" alone couldn't say whether
an album was matched to "Far & Off" or "Far & Off (24bits)". Every name comes
from the stored release; nothing here may ask MusicBrainz.
"""

from bs4 import BeautifulSoup

from harmonist import mb_cache, scanner
from harmonist import sidecar as sc
from harmonist.models import MatchCandidate, Sidecar
from test import test_web
from test.test_web import (
    _confirmation_setup,
    _id_for,
    _make_album,
    _make_tagged_album,
    _release_for_match,
)

cfg = test_web.cfg
client = test_web.client


def _store(monkeypatch, mbid: str, disambiguation: str = "24bits") -> None:
    """Put a release in the store, as a lookup or a tagging would have."""
    release = _release_for_match(mbid, n_tracks=1) | {"disambiguation": disambiguation}
    monkeypatch.setattr("harmonist.mb_lookup.fetch_release", lambda _mbid: release)
    mb_cache.fetch_release(mbid)


def _links_to(html: str, mbid: str) -> list[str]:
    """The text of every link to this release, whitespace collapsed."""
    soup = BeautifulSoup(html, "html.parser")
    return [
        " ".join(a.get_text().split())
        for a in soup.find_all("a", href=f"https://musicbrainz.org/release/{mbid}")
    ]


def test_the_review_names_the_release_it_would_confirm(client, cfg, monkeypatch):
    root, release, *_ = _confirmation_setup(cfg, monkeypatch, old_mbid=None)
    release["disambiguation"] = "24bits"

    editor = client.get(f"/assignments/{_id_for(cfg, root)}")

    heading = BeautifulSoup(editor.text, "html.parser").find("h3")
    assert heading is not None and "Suggested match" in heading.get_text()
    assert _links_to(str(heading), release["id"]) == ["Title (24bits) ↗"]


def test_album_identity_uses_the_stored_release_name_without_a_request(client, cfg, monkeypatch):
    d = _make_tagged_album(cfg, "Local title", mbid="rel-named", tagged_at=None)
    _store(monkeypatch, "rel-named")
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release",
        lambda _mbid: (_ for _ in ()).throw(AssertionError("asked MusicBrainz")),
    )
    body = client.get(f"/album/{_id_for(cfg, d)}").text
    identity = BeautifulSoup(body, "html.parser").select_one("#album-identity")
    assert _links_to(str(identity), "rel-named") == ["Title (24bits) ↗"]


def test_a_surrendered_album_names_its_tagged_release(client, cfg, monkeypatch):
    _store(monkeypatch, "rel-surr")
    d = _make_album(cfg, "Surrendered")
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/surrendered",
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-surr",
                confidence="exact",
                file_count=1,
                track_count=1,
                unmatched_purchase=True,
            ),
        ),
    )
    scanner.scan(cfg.paths.music_dir)

    assert "Title (24bits) ↗" in _links_to(client.get("/tasks").text, "rel-surr")


def test_an_inbox_card_names_the_release_its_album_is_matched_to(client, cfg, monkeypatch):
    """The card already names the album, so its pill carries only what tells
    the release from its siblings."""
    _store(monkeypatch, "rel-sync")
    d = _make_tagged_album(cfg, "To Link", mbid="rel-sync", tagged_at=None)
    sc.write(
        d,
        Sidecar(store_url="https://x.bandcamp.com/album/to-link", mb_release_id="rel-sync"),
    )
    scanner.scan(cfg.paths.music_dir)

    assert "MusicBrainz · 24bits ↗" in _links_to(client.get("/tasks").text, "rel-sync")


def test_with_nothing_stored_the_release_is_still_linked(client, cfg, monkeypatch):
    """Naming a release never costs a MusicBrainz request: with nothing stored,
    the link is the pill it always was."""
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release",
        lambda _mbid: (_ for _ in ()).throw(AssertionError("asked MusicBrainz")),
    )
    d = _make_album(cfg, "Unstored")
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/unstored",
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-unstored",
                confidence="exact",
                file_count=1,
                track_count=1,
                unmatched_purchase=True,
            ),
        ),
    )
    scanner.scan(cfg.paths.music_dir)

    assert _links_to(client.get("/tasks").text, "rel-unstored") == ["MusicBrainz ↗"]
