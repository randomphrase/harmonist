"""Artist NFOs that no album credits are artists too, identified or not (#936)."""

import pytest
from bs4 import BeautifulSoup

from harmonist import artist_nfo, id_registry
from test.test_artist_image_selection import picture
from test.test_artist_web import ALICE, RELEASE, artist_client
from test.test_nfo_inventory import write_nfo

__all__ = ["artist_client"]

CAROL = "77777777-7777-4777-8777-777777777777"
VARIOUS = "89ad4ac3-39f7-470e-963a-56509c546377"


@pytest.mark.parametrize(
    "content",
    [b"", b"  \n\t\r\n", b"\xef\xbb\xbf", b"\xef\xbb\xbf\n", b"\xff\xfe \x00\n\x00"],
    ids=["zero bytes", "whitespace", "utf-8 bom", "utf-8 bom and newline", "utf-16 whitespace"],
)
def test_an_empty_nfo_reads_as_waiting_for_identification(tmp_path, content):
    path = tmp_path / "artist.nfo"
    path.write_bytes(content)
    profile = artist_nfo.read(path)
    assert (profile.mbid, profile.name, profile.facts, profile.biography) == (None, "", (), "")


@pytest.mark.parametrize("content", [b"<artist>", b"\xff\xff", b"<album/>"])
def test_a_file_with_content_still_has_to_be_an_artist_document(tmp_path, content):
    path = tmp_path / "artist.nfo"
    path.write_bytes(content)
    with pytest.raises(artist_nfo.NfoError):
        artist_nfo.read(path)


@pytest.fixture
def nfo_only(artist_client):
    """Alice and Bob are credited by albums; Carol, Dave and Eve only have NFOs."""
    client, cfg, network = artist_client
    root = cfg.paths.music_dir
    carol = write_nfo(root, "metadata/uncredited", CAROL, "Carol")
    dave = root / "Dave" / "artist.nfo"
    dave.parent.mkdir()
    dave.write_bytes(b"")
    eve = write_nfo(root, "metadata/no-id", None, "Eve")
    write_nfo(root, "metadata/various", VARIOUS, "Various Artists")
    return client, cfg, network, {"carol": carol, "dave": dave, "eve": eve}


def test_nfos_no_album_credits_are_listed_with_no_albums(nfo_only):
    client, _cfg, network, paths = nfo_only
    response = client.get("/artists")
    rows = response.context["artists_view"]["rows"]
    assert [(a.name, len(a.albums)) for a in rows] == [
        ("Alice", 2),
        ("Bob", 1),
        ("Carol", 0),
        ("Dave", 0),
        ("Eve", 0),
    ]
    by_name = {a.name: a for a in rows}
    assert (by_name["Carol"].key, by_name["Carol"].mbid) == (CAROL, CAROL)
    # An NFO without an artist ID describes no one yet, so its path is its
    # handle, exactly as for an album without a sidecar.
    for name in ("Dave", "Eve"):
        path = paths[name.lower()]
        assert (by_name[name].key, by_name[name].mbid) == (id_registry.get_or_mint(path), None)
        assert by_name[name].nfo_paths == (path,)
    page = BeautifulSoup(response.text, "html.parser")
    assert page.select_one("#artists-count").get_text() == "5"
    assert [
        a.name for a in client.get("/artists?artist_q=dav").context["artists_view"]["rows"]
    ] == ["Dave"]
    network.assert_not_called()


def test_artists_without_an_id_are_marked_and_filterable(nfo_only):
    client, _cfg, _network, _paths = nfo_only
    view = client.get("/artists").context["artists_view"]
    badges = {a.name: view["badges"][a.key] for a in view["rows"]}
    assert badges == {
        "Alice": (),
        "Bob": (),
        "Carol": (),
        "Dave": ("Not identified",),
        "Eve": ("Not identified",),
    }

    def names(query):
        return [a.name for a in client.get(f"/artists?{query}").context["artists_view"]["rows"]]

    assert names("albums=none") == ["Carol", "Dave", "Eve"]
    assert names("albums=with") == ["Alice", "Bob"]
    assert names("nfo=unidentified") == ["Dave", "Eve"]
    assert names("nfo=unidentified&albums=with") == []
    facets = {f["key"]: f for f in view["facets"]}
    assert [(c["label"], c["count"]) for c in facets["albums"]["choices"]] == [
        ("With albums", 2),
        ("No albums", 3),
    ]
    assert ("Not identified", 2) in [(c["label"], c["count"]) for c in facets["nfo"]["choices"]]


def test_an_nfo_only_artist_has_a_page_without_an_albums_section(nfo_only):
    client, _cfg, network, paths = nfo_only
    carol = client.get(f"/artist/{CAROL}")
    assert carol.status_code == 200
    assert carol.context["nfo"].profile.path == paths["carol"]
    assert f"https://musicbrainz.org/artist/{CAROL}" in carol.text
    page = BeautifulSoup(carol.text, "html.parser")
    assert page.select_one('section[aria-labelledby="artist-albums-heading"]') is None
    assert page.select_one("#artist-detail header .album-warning__badge") is None
    alice = BeautifulSoup(client.get(f"/artist/{ALICE}").text, "html.parser")
    assert alice.select_one('section[aria-labelledby="artist-albums-heading"]') is not None
    network.assert_not_called()


def test_an_unidentified_nfo_has_a_page_keyed_by_its_path(nfo_only):
    client, _cfg, network, paths = nfo_only
    key = id_registry.get_or_mint(paths["eve"])
    response = client.get(f"/artist/{key}")
    assert response.status_code == 200
    assert response.context["artist"].mbid is None
    assert response.context["nfo"].profile.path == paths["eve"]
    page = BeautifulSoup(response.text, "html.parser")
    assert page.select_one("#artist-detail h1").get_text(strip=True) == "Eve"
    assert [
        b.get_text(strip=True) for b in page.select("#artist-detail header .album-warning__badge")
    ] == ["Not identified"]
    assert "musicbrainz.org/artist/" not in response.text
    empty = client.get(f"/artist/{id_registry.get_or_mint(paths['dave'])}")
    assert empty.status_code == 200
    assert empty.context["nfo"].errors == ()
    paths["eve"].rename(paths["eve"].with_name("moved.nfo"))
    assert client.get(f"/artist/{key}").status_code == 404
    assert client.get("/artist/" + "0" * 32).status_code == 404
    network.assert_not_called()


@pytest.mark.parametrize("engaged", [False, True], ids=["request scan", "background scan"])
def test_an_unidentified_nfo_serves_its_local_picture(nfo_only, monkeypatch, engaged):
    client, _cfg, _network, paths = nfo_only
    image = picture(paths["eve"].parent, "artist.png")
    if engaged:
        runner = client.app.state.scan_runner
        runner.refresh_now()
        monkeypatch.setattr(runner, "is_engaged", lambda: True)
    key = id_registry.get_or_mint(paths["eve"])
    view = client.get("/artists").context["artists_view"]
    assert view["image_urls"][key].startswith(f"/artist/{key}/image?v=")
    response = client.get(f"/artist/{key}/image")
    assert (response.status_code, response.content) == (200, image.read_bytes())


def test_two_nfos_with_one_uncredited_id_are_one_conflicted_artist(nfo_only):
    client, cfg, _network, paths = nfo_only
    second = write_nfo(cfg.paths.music_dir, "metadata/uncredited-copy", CAROL, "Carol")
    view = client.get("/artists").context["artists_view"]
    carol = [a for a in view["rows"] if a.mbid == CAROL]
    assert [(a.name, a.nfo_paths) for a in carol] == [("Carol", (paths["carol"], second))]
    assert view["badges"][CAROL] == ("NFO conflict",)


def test_the_albums_filter_survives_artist_and_album_navigation(nfo_only):
    client, _cfg, _network, _paths = nfo_only
    detail = client.get(f"/artist/{ALICE}?artist_albums=with")
    assert "albums=with" in detail.context["artist_back_url"]
    assert "artist_albums=with" in detail.context["artist_album_query"]
    album = client.get(f"/album/{RELEASE}?from_artist={ALICE}&artist_albums=with")
    assert "artist_albums=with" in album.context["artist_back_url"]
