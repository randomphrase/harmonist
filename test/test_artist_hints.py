"""The artists an NFO's folder suggests, for its Inbox card (#996)."""

from unittest.mock import Mock

from harmonist import artist_nfo
from harmonist.artists import Hint, Hints, unidentified
from harmonist.models import Album, AlbumArtist, AlbumState, Sidecar
from test.test_nfo_inventory import write_nfo

ALICE = "11111111-1111-4111-8111-111111111111"
BOB = "22222222-2222-4222-8222-222222222222"
CAROL = "77777777-7777-4777-8777-777777777777"
VARIOUS = "89ad4ac3-39f7-470e-963a-56509c546377"
TOGETHER = "33333333-3333-4333-8333-333333333333"


def album(root, folder, *credits, release=None):
    return Album(
        id=folder,
        path=root / folder,
        title=folder,
        artist="display",
        track_count=1,
        state=AlbumState.COMPLETE,
        artist_credits=credits,
        sidecar=Sidecar(mb_release_id=release) if release else None,
    )


def nothing_stored(_release):
    return None


def hints_for(root, folder, albums, stored=nothing_stored):
    """The hints on the card of an empty NFO in `folder`."""
    nfo = root / folder / "artist.nfo"
    nfo.parent.mkdir(parents=True, exist_ok=True)
    nfo.write_bytes(b"")
    inventory = artist_nfo.Inventory(profiles=(artist_nfo.read(nfo),))
    (card,) = unidentified(inventory, albums, root, stored)
    return card.hints


def credit(artist_id, name, disambiguation="", credited_as=None):
    """One `artist-credit` entry of a stored release, as musicbrainzngs gives it."""
    return {
        "artist": {"id": artist_id, "name": name, "disambiguation": disambiguation},
        "name": credited_as or name,
    }


def test_the_artist_every_album_in_the_folder_credits_is_the_suggestion(tmp_path):
    albums = [
        album(tmp_path, "Alice/One", AlbumArtist("Alice", ALICE)),
        album(tmp_path, "Alice/Two", AlbumArtist("Alice", ALICE), AlbumArtist("Bob", BOB)),
    ]
    found = hints_for(tmp_path, "Alice", albums)
    assert found == Hints(2, (Hint(ALICE, "Alice", 2), Hint(BOB, "Bob", 1)))
    assert found.suggestion == Hint(ALICE, "Alice", 2)


def test_without_one_artist_on_every_album_there_are_only_candidates(tmp_path):
    albums = [
        album(tmp_path, "Mixed/One", AlbumArtist("Bob", BOB)),
        album(tmp_path, "Mixed/Two", AlbumArtist("Alice", ALICE)),
        album(tmp_path, "Mixed/Three", AlbumArtist("Alice", ALICE)),
    ]
    found = hints_for(tmp_path, "Mixed", albums)
    assert found.artists == (Hint(ALICE, "Alice", 2), Hint(BOB, "Bob", 1))
    assert found.suggestion is None


def test_two_artists_on_every_album_are_both_candidates_and_neither_the_suggestion(tmp_path):
    duo = (AlbumArtist("Bob", BOB), AlbumArtist("Alice", ALICE))
    albums = [album(tmp_path, "Duo/One", *duo), album(tmp_path, "Duo/Two", *duo)]
    found = hints_for(tmp_path, "Duo", albums)
    assert found.artists == (Hint(ALICE, "Alice", 2), Hint(BOB, "Bob", 2))
    assert found.suggestion is None


def test_only_albums_beneath_the_folder_count_however_deep(tmp_path):
    albums = [
        album(tmp_path, "Alice/Live/1999", AlbumArtist("Alice", ALICE)),
        # A sibling whose name starts the same is not beneath it.
        album(tmp_path, "Alice Cooper/One", AlbumArtist("Bob", BOB)),
        album(tmp_path, "Other/Alice", AlbumArtist("Carol", CAROL)),
    ]
    assert hints_for(tmp_path, "Alice", albums) == Hints(1, (Hint(ALICE, "Alice", 1),))


def test_an_nfo_in_an_album_folder_takes_that_album(tmp_path):
    albums = [album(tmp_path, "Alice/One", AlbumArtist("Alice", ALICE))]
    assert hints_for(tmp_path, "Alice/One", albums).suggestion == Hint(ALICE, "Alice", 1)


def test_special_purpose_artists_are_never_hints_but_their_albums_count(tmp_path):
    albums = [
        album(tmp_path, "Alice/One", AlbumArtist("Alice", ALICE)),
        album(tmp_path, "Alice/Tribute", AlbumArtist("Various Artists", VARIOUS)),
    ]
    found = hints_for(tmp_path, "Alice", albums)
    assert found == Hints(2, (Hint(ALICE, "Alice", 1),))
    assert found.suggestion is None


def test_a_credit_without_an_id_suggests_nothing_but_its_album_counts(tmp_path):
    albums = [
        album(tmp_path, "Alice/One", AlbumArtist("Alice", ALICE)),
        album(tmp_path, "Alice/Two", AlbumArtist("Alice")),
    ]
    found = hints_for(tmp_path, "Alice", albums)
    assert found == Hints(2, (Hint(ALICE, "Alice", 1),))
    assert found.suggestion is None


def test_an_nfo_at_the_top_of_the_library_gets_no_hints(tmp_path):
    albums = [album(tmp_path, "Alice/One", AlbumArtist("Alice", ALICE))]
    assert hints_for(tmp_path, "", albums) == Hints(0, ())


def test_a_folder_without_albums_gets_no_hints(tmp_path):
    albums = [album(tmp_path, "Alice/One", AlbumArtist("Alice", ALICE))]
    found = hints_for(tmp_path, "metadata/Alice", albums)
    assert found == Hints(0, ())
    assert found.suggestion is None


def test_only_files_without_an_artist_id_wait_in_the_inbox(tmp_path):
    named = write_nfo(tmp_path, "Zed", None, "Zed")
    empty = tmp_path / "Alice" / "artist.nfo"
    empty.parent.mkdir()
    empty.write_bytes(b"")
    write_nfo(tmp_path, "Carol", CAROL, "Carol")
    inventory = artist_nfo.Inventory(
        profiles=tuple(artist_nfo.read(p) for p in sorted(tmp_path.glob("*/artist.nfo")))
    )
    albums = [album(tmp_path, "Alice/One", AlbumArtist("Alice", ALICE))]
    cards = unidentified(inventory, albums, tmp_path, nothing_stored)
    # Named by the file, or by its folder while it names no one.
    assert [(c.name, c.profile.path) for c in cards] == [("Alice", empty), ("Zed", named)]
    assert [c.key for c in cards] == [c.profile.key for c in cards]
    assert cards[0].hints.suggestion == Hint(ALICE, "Alice", 1)
    assert cards[1].hints == Hints(0, ())


def test_a_stored_release_names_each_artist_with_its_disambiguation(tmp_path):
    albums = [
        album(
            tmp_path,
            "Zake/Together",
            AlbumArtist("zake", ALICE),
            AlbumArtist("B.", BOB),
            release=TOGETHER,
        ),
    ]
    releases = {
        TOGETHER: {
            "artist-credit": [
                credit(ALICE, "Zake", "ambient, US", credited_as="zake"),
                " & ",
                credit(BOB, "Bob", "drone"),
            ]
        }
    }
    stored = Mock(side_effect=releases.get)
    found = hints_for(tmp_path, "Zake", albums, stored)
    # MusicBrainz's own name, not how the release credits it, as a search lists it.
    assert found.artists == (
        Hint(BOB, "Bob", 1, "drone"),
        Hint(ALICE, "Zake", 1, "ambient, US"),
    )
    # One read for the release both artists are named from.
    stored.assert_called_once_with(TOGETHER)


def test_without_a_stored_release_the_tag_names_the_artist(tmp_path):
    albums = [
        album(tmp_path, "Zake/One", AlbumArtist("zake", ALICE), release=TOGETHER),
        album(tmp_path, "Zake/Two", AlbumArtist("zake", ALICE)),
    ]
    # A stored release that credits someone else says nothing about this one.
    elsewhere = {"artist-credit": [credit(BOB, "Bob", "drone")]}
    assert hints_for(tmp_path, "Zake", albums, lambda _r: elsewhere).artists == (
        Hint(ALICE, "zake", 2),
    )


def test_the_token_changes_only_with_what_the_folder_offers(tmp_path):
    one = [album(tmp_path, "Zake/One", AlbumArtist("Zake", ALICE))]
    two = [*one, album(tmp_path, "Zake/Two", AlbumArtist("Bob", BOB))]
    assert hints_for(tmp_path, "Zake", one).token == hints_for(tmp_path, "Zake", list(one)).token
    assert hints_for(tmp_path, "Zake", one).token != hints_for(tmp_path, "Zake", two).token
