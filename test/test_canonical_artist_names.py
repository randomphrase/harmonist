"""What Harmonist calls an artist (#1001), and how it tells two of one name
apart (#982). The canonical name is the NFO's, else the tags'; a comment comes
from the same source, else from a stored release, and only where shared."""

from unittest.mock import Mock

import pytest
from bs4 import BeautifulSoup

from harmonist import artist_nfo, mb_cache
from harmonist.artists import build_index, disambiguate
from harmonist.models import Album, AlbumArtist, AlbumState, Sidecar
from test.test_artist_web import ALICE, BOB, SOLO, artist_client, make_album

__all__ = ["artist_client"]

# Ordered so that sorting by ID puts the Namlook project first, and sorting by
# comment puts the French band first.
NAMLOOK = "11111111-1111-4111-8111-111111111111"
FRENCH = "22222222-2222-4222-8222-222222222222"
MASSIVE = "33333333-3333-4333-8333-333333333333"
GYBE = "44444444-4444-4444-8444-444444444444"


def album(root, folder, name, artist_id, release=None, sort=None):
    return Album(
        id=folder,
        path=root / folder,
        title=folder,
        artist=name,
        track_count=1,
        state=AlbumState.COMPLETE,
        artist_credits=(AlbumArtist(name, artist_id, sort),),
        sidecar=Sidecar(mb_release_id=release) if release else None,
    )


def release(artist_id, name, disambiguation=""):
    """A stored release crediting one artist, as musicbrainzngs gives it."""
    return {
        "artist-credit": [
            {"artist": {"id": artist_id, "name": name, "disambiguation": disambiguation}}
        ]
    }


def nfo(root, folder, artist_id, *, name="Air", disambiguation=None, sortname=None):
    path = root / folder / "artist.nfo"
    path.parent.mkdir(parents=True, exist_ok=True)
    elements = [f"<musicbrainzartistid>{artist_id}</musicbrainzartistid>"]
    if name:
        elements.append(f"<name>{name}</name>")
    if sortname:
        elements.append(f"<sortname>{sortname}</sortname>")
    if disambiguation:
        elements.append(f"<disambiguation>{disambiguation}</disambiguation>")
    path.write_text(f"<artist>{''.join(elements)}</artist>")
    return artist_nfo.read(path)


def inventory(*profiles):
    return artist_nfo.Inventory(profiles=profiles)


def nothing_stored(_release):
    return None


def shown(index):
    return [(a.name, a.disambiguation) for a in index]


def two_airs(root):
    return [
        album(root, "Air", "Air", NAMLOOK, "rel-namlook"),
        album(root, "Moon Safari", "Air", FRENCH, "rel-moon"),
        album(root, "Mezzanine", "Massive Attack", MASSIVE, "rel-mezzanine"),
    ]


# The canonical name


def test_an_nfo_names_the_artist_it_describes_and_the_tags_name_the_rest(tmp_path):
    albums = [
        album(tmp_path, "Yanqui U.X.O.", "GY!BE", GYBE, sort="GY!BE"),
        album(tmp_path, "Mezzanine", "Massive Attack", MASSIVE, sort="Massive Attack"),
    ]
    files = inventory(
        nfo(
            tmp_path,
            "GYBE",
            GYBE,
            name="Godspeed You! Black Emperor",
            sortname="Godspeed You Black Emperor",
        )
    )
    named = {a.key: (a.name, a.sort_name) for a in build_index(albums, files)}
    assert named == {
        GYBE: ("Godspeed You! Black Emperor", "Godspeed You Black Emperor"),
        MASSIVE: ("Massive Attack", "Massive Attack"),
    }


def test_an_nfo_without_a_sort_name_gives_none(tmp_path):
    albums = [album(tmp_path, "Yanqui U.X.O.", "GY!BE", GYBE, sort="GY!BE")]
    files = inventory(nfo(tmp_path, "GYBE", GYBE, name="Godspeed You! Black Emperor"))
    (artist,) = build_index(albums, files)
    assert (artist.name, artist.sort_name) == ("Godspeed You! Black Emperor", None)


@pytest.mark.parametrize(
    "nfo_names", [("",), ("Godspeed You! Black Emperor", "Godspeed You Black Emperor!")]
)
def test_the_tags_name_an_artist_whose_nfos_give_no_one_name(tmp_path, nfo_names):
    """An NFO with no name, or two that disagree, name no one: the tags do,
    sort name and all."""
    albums = [album(tmp_path, "Yanqui U.X.O.", "GY!BE", GYBE, sort="GY!BE")]
    files = inventory(
        *(
            nfo(tmp_path, f"GYBE {i}", GYBE, name=name, sortname="Godspeed")
            for i, name in enumerate(nfo_names)
        )
    )
    (artist,) = build_index(albums, files)
    assert (artist.name, artist.sort_name) == ("GY!BE", "GY!BE")


# Disambiguation


def test_a_shared_name_takes_the_comment_from_a_stored_release(tmp_path):
    stored = {
        "rel-namlook": release(NAMLOOK, "Air", "Pete Namlook's ambient project"),
        "rel-moon": release(FRENCH, "Air", "French band"),
        "rel-mezzanine": release(MASSIVE, "Massive Attack", "Bristol trip hop group"),
    }
    result = disambiguate(build_index(two_airs(tmp_path)), stored.get)
    assert shown(result) == [
        ("Air", "French band"),
        ("Air", "Pete Namlook's ambient project"),
        ("Massive Attack", ""),
    ]


def test_a_name_no_one_else_has_reads_nothing(tmp_path):
    index = build_index([album(tmp_path, "Mezzanine", "Massive Attack", MASSIVE, "rel")])
    stored = Mock(return_value=release(MASSIVE, "Massive Attack", "Bristol trip hop group"))
    assert shown(disambiguate(index, stored)) == [("Massive Attack", "")]
    stored.assert_not_called()


def test_names_are_shared_under_the_fold_the_index_sorts_by(tmp_path):
    albums = [
        album(tmp_path, "Air", "Air", NAMLOOK, "rel-namlook"),
        album(tmp_path, "Moon Safari", "AIR", FRENCH, "rel-moon"),
    ]
    stored = {
        "rel-namlook": release(NAMLOOK, "Air", "Pete Namlook's ambient project"),
        "rel-moon": release(FRENCH, "AIR", "French band"),
    }
    assert shown(disambiguate(build_index(albums), stored.get)) == [
        ("AIR", "French band"),
        ("Air", "Pete Namlook's ambient project"),
    ]


def test_names_are_shared_as_they_are_shown(tmp_path):
    """An NFO's name can be the one another artist shares."""
    albums = [
        album(tmp_path, "Air", "Air", NAMLOOK, "rel-namlook"),
        album(tmp_path, "Moon Safari", "Air French Band", FRENCH, "rel-moon"),
    ]
    files = inventory(nfo(tmp_path, "AIR", FRENCH, name="Air", disambiguation="French band"))
    stored = {"rel-namlook": release(NAMLOOK, "Air", "Pete Namlook's ambient project")}
    assert shown(disambiguate(build_index(albums, files), stored.get, files)) == [
        ("Air", "French band"),
        ("Air", "Pete Namlook's ambient project"),
    ]


def test_an_nfo_gives_the_comment_rather_than_a_stored_release(tmp_path):
    files = inventory(nfo(tmp_path, "AIR", FRENCH, disambiguation="French band"))
    stored = {"rel-moon": release(FRENCH, "Air", "Paris duo")}
    index = build_index(two_airs(tmp_path), files)
    result = {a.key: a.disambiguation for a in disambiguate(index, stored.get, files)}
    assert result[FRENCH] == "French band"


def test_an_nfo_without_a_comment_gives_none_whatever_musicbrainz_says(tmp_path):
    files = inventory(nfo(tmp_path, "AIR", FRENCH))
    stored = {"rel-moon": release(FRENCH, "Air", "French band")}
    index = build_index(two_airs(tmp_path), files)
    result = {a.key: a.disambiguation for a in disambiguate(index, stored.get, files)}
    assert result[FRENCH] == ""


@pytest.mark.parametrize(
    ("second", "expected"), [("French band", "French band"), ("Paris duo", "")]
)
def test_nfos_give_a_comment_only_when_they_agree(tmp_path, second, expected):
    files = inventory(
        nfo(tmp_path, "AIR", FRENCH, disambiguation="French band"),
        nfo(tmp_path, "Elsewhere/AIR", FRENCH, disambiguation=second),
    )
    index = build_index(two_airs(tmp_path), files)
    result = {a.key: a.disambiguation for a in disambiguate(index, nothing_stored, files)}
    assert result[FRENCH] == expected


def test_with_no_source_the_name_stands_alone(tmp_path):
    stored = {"rel-moon": release(FRENCH, "Air", "French band")}
    index = build_index(two_airs(tmp_path))
    result = {a.key: a.disambiguation for a in disambiguate(index, stored.get)}
    assert result == {FRENCH: "French band", NAMLOOK: "", MASSIVE: ""}


# The pages

OTHER_ALICE = "66666666-6666-4666-8666-666666666666"
OTHER_RELEASE = "77777777-7777-4777-8777-777777777777"


def rename_alice(cfg, name="Alice", disambiguation=None):
    """Rewrite the artist client's NFO for Alice."""
    path = cfg.paths.music_dir / "ArtistInfo" / "Different directory name" / "artist.nfo"
    comment = f"<disambiguation>{disambiguation}</disambiguation>" if disambiguation else ""
    path.write_text(
        f"<artist><name>{name}</name><musicbrainzartistid>{ALICE}</musicbrainzartistid>"
        f"{comment}</artist>"
    )


@pytest.fixture
def two_alices(artist_client, monkeypatch):
    """The artist client's Alice, whose NFO calls her the singer, and Bob; and
    another Alice with no NFO. MusicBrainz's comments are stored for each
    Alice's releases, and its comment on the first Alice is a different one."""
    client, cfg, network = artist_client
    make_album(cfg.paths.music_dir, "Other Alice", OTHER_RELEASE, ["Alice"], [OTHER_ALICE])
    rename_alice(cfg, disambiguation="the singer")
    stored = {
        SOLO: release(ALICE, "Alice", "the vocalist"),
        OTHER_RELEASE: release(OTHER_ALICE, "Alice", "the drummer"),
    }
    monkeypatch.setattr(mb_cache, "stored_release", stored.get)
    return client, network


def names(html):
    """Each artist card's name, as the list draws it."""
    page = BeautifulSoup(html, "html.parser")
    return [
        (card["href"].split("?")[0], card.select_one("span.text-sm.text-ink"))
        for card in page.select("a.artist-card")
    ]


def test_the_list_tells_two_artists_of_one_name_apart(two_alices):
    client, network = two_alices
    listed = names(client.get("/artists").text)
    assert [(href, n.get_text(" ", strip=True)) for href, n in listed] == [
        (f"/artist/{OTHER_ALICE}", "Alice (the drummer)"),
        (f"/artist/{ALICE}", "Alice (the singer)"),
        (f"/artist/{BOB}", "Bob"),
    ]
    network.assert_not_called()


def test_a_card_names_the_artist_in_full_in_its_tooltip(two_alices):
    client, _network = two_alices
    listed = names(client.get("/artists?view=cards").text)
    assert [n["title"] for _href, n in listed] == [
        "Alice (the drummer)",
        "Alice (the singer)",
        "Bob",
    ]


def test_the_artist_page_names_the_artist_and_links_musicbrainz_by_its_own_name(two_alices):
    client, network = two_alices

    def header(key):
        page = BeautifulSoup(client.get(f"/artist/{key}").text, "html.parser")
        link = page.select_one(f'#artist-detail a[href="https://musicbrainz.org/artist/{key}"]')
        return (
            page.select_one("#artist-detail h1").get_text(" ", strip=True),
            link.get_text(" ", strip=True),
        )

    assert header(ALICE) == ("Alice (the singer)", "Alice (the vocalist) ↗")
    assert header(OTHER_ALICE) == ("Alice (the drummer)", "Alice (the drummer) ↗")
    assert header(BOB) == ("Bob", "MusicBrainz ↗")
    network.assert_not_called()


def test_the_nfo_names_the_artist_and_an_album_credit_keeps_its_own_words(artist_client):
    client, cfg, network = artist_client
    rename_alice(cfg, name="Alice Cooper")
    listed = names(client.get("/artists").text)
    assert [n.get_text(" ", strip=True) for _href, n in listed] == ["Alice Cooper", "Bob"]
    page = BeautifulSoup(client.get(f"/artist/{ALICE}").text, "html.parser")
    assert page.select_one("#artist-detail h1").get_text(" ", strip=True) == "Alice Cooper"

    def credit_links(html, selector):
        links = BeautifulSoup(html, "html.parser").select(selector)
        return {a["href"]: (a.get_text(), a.get("title")) for a in links}

    tiles = credit_links(client.get("/library").text, "a.library-artist-link")
    assert tiles[f"/artist/{ALICE}"] == ("Alice", "Alice Cooper")
    assert tiles[f"/artist/{BOB}"] == ("Bob", None)
    heading = credit_links(client.get(f"/album/{SOLO}").text, "h1 a.app-link")
    assert heading == {f"/artist/{ALICE}": ("Alice", "Alice Cooper")}
    network.assert_not_called()
