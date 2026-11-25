"""The public demo exercises the real artist inventory and exclusion rules."""

import asyncio
from functools import partial

import pytest

from harmonist import (
    artist_fill,
    artist_nfo,
    artists,
    demo,
    mb_lookup,
    nfo_writer,
    scanner,
    wikimedia,
)
from harmonist.models import AlbumState
from harmonist.web.artist_work import ArtistWork

VARIOUS = "89ad4ac3-39f7-470e-963a-56509c546377"


def test_demo_inventory_has_a_portrait_conflicts_and_unmatched_profiles(tmp_path):
    root = tmp_path / "music"
    demo.seed(root)
    albums = scanner.scan(root)
    inventory = artist_nfo.scan(root)
    barry = demo.MB_RELEASES["demo-rel-barryjive"]["artist-credit"][0]["artist"]["id"]
    mouse = demo.MB_RELEASES["demo-rel-mouserat"]["artist-credit"][0]["artist"]["id"]
    profile = inventory.for_artist(barry).profile
    assert profile is not None
    assert profile.name == "Barry Jive and the Uptown Five"
    assert profile.biography and profile.image_ref == "artist.png"
    assert profile.image_path is not None
    assert profile.image_path.read_bytes() == (demo.ASSETS_DIR / "barry.png").read_bytes()
    conflict = inventory.for_artist(mouse)
    assert conflict.profile is None
    assert len(conflict.candidates) == 2
    assert all(p.name == "artist.nfo" for p in conflict.candidates)
    assert len(inventory.paths) == 6
    assert not inventory.errors and not inventory.walk_errors
    credited = {c.mbid for a in albums for c in a.artist_credits if c.mbid}
    unmatched = [p for p in inventory.profiles if p.mbid not in credited]
    assert {p.name for p in unmatched} == {"The Unidentified Echoes", "The Unplayed Satellites", ""}
    assert sum(p.mbid is None for p in unmatched) == 2
    # The empty file sits in a credited artist's own folder, and still names no one.
    listed = artists.build_index(
        [a for a in albums if a.state in {AlbumState.COMPLETE, AlbumState.INCOMPLETE}], inventory
    )
    assert [a.mbid for a in listed if a.name == "Wyld Stallion"].count(None) == 1


def test_demo_compilation_uses_the_official_various_artists_identity(tmp_path):
    root = tmp_path / "music"
    demo.seed(root)
    albums = scanner.scan(root)
    compilation = next(a for a in albums if a.title == "The Rural Juror (OST)")
    assert compilation.artist_credits[0].mbid == VARIOUS
    release = demo.MB_RELEASES["demo-rel-rural-juror"]
    assert release["artist-credit"][0]["artist"]["id"] == VARIOUS
    assert VARIOUS not in {a.key for a in artists.build_index(albums)}


def test_demo_nfo_definitions_participate_in_the_dataset_version(monkeypatch):
    previous = demo.data_version()
    monkeypatch.setattr(
        demo,
        "ARTIST_NFOS",
        ({"directory": "metadata/changed", "name": "Changed fixture"},),
        raising=False,
    )
    assert demo.data_version() != previous


def test_demo_resets_and_old_dataset_upgrade_restore_identical_nfo_fixtures(tmp_path):
    root = tmp_path / "music"
    demo.seed(root)

    def metadata():
        paths = list(root.rglob("artist.nfo"))
        assert len(paths) == 6
        pictures = [p.parent / "artist.png" for p in paths if (p.parent / "artist.png").exists()]
        return {str(p.relative_to(root)): p.read_bytes() for p in paths + pictures}

    original = metadata()
    for _ in range(2):
        demo.reset(root)
        assert metadata() == original
    for file in root.rglob("artist.nfo"):
        file.unlink()
    (root / demo.DEMO_MARKER).write_text("Harmonist demo data\nversion: before-artists\n")
    assert demo.ensure_seeded(root)
    assert metadata() == original
    assert demo._marker_version(root) == demo.data_version()


def test_the_demos_musicbrainz_knows_each_credited_artist_and_no_other():
    """So the empty Wyld Stallion NFO can be filled in from its album's credit (#981)."""
    wyld = demo.MB_RELEASES["demo-rel-wyld"]["artist-credit"][0]["artist"]["id"]
    answer = demo.fetch_artist(wyld)
    assert nfo_writer.mapped(answer) == {
        "name": "Wyld Stallion",
        "musicbrainzartistid": wyld,
        "sortname": "Wyld Stallion",
        "type": "Group",
        "gender": None,
        "disambiguation": "San Dimas rock duo",
        "formed": "1988",
        "disbanded": None,
    }
    with pytest.raises(mb_lookup.ArtistNotFoundError):
        demo.fetch_artist("00000000-0000-4000-8000-000000000000")


def test_the_demos_wikipedia_has_a_biography_for_wyld_stallion_and_is_never_asked_for_real():
    """So identifying the empty NFO fills in a biography too (#1018), without
    the demo ever contacting Wikimedia."""
    demo.install()
    assert wikimedia.biography.__module__ == demo.__name__
    wyld = demo.MB_RELEASES["demo-rel-wyld"]["artist-credit"][0]["artist"]["id"]
    item = wikimedia.wikidata_item(demo.fetch_artist(wyld))
    assert item is not None

    async def biography(languages):
        work = ArtistWork()
        try:
            return await wikimedia.biography(
                item, languages=languages, user_agent="demo", client=work.http
            )
        finally:
            await work.close()

    found = asyncio.run(biography(["en"]))
    assert found is not None and found.text.startswith("Wyld Stallion are a rock duo")
    assert asyncio.run(biography(["pt"])) is None


def test_the_demos_pictures_come_from_each_source_and_never_for_real(tmp_path):
    """Identifying Wyld Stallion fills in fanart.tv's picture, reading The
    Unplayed Satellites again fills in Commons', and Mouse Rat is on neither
    (#1020), without the demo ever contacting fanart.tv or Wikimedia."""
    demo.install()
    asking = artist_fill.Asking(languages=["en"], user_agent="demo")
    wyld = demo.MB_RELEASES["demo-rel-wyld"]["artist-credit"][0]["artist"]["id"]
    satellites = demo._demo_mbid("art", "demo-unplayed-satellites")
    mouse_rat = demo.MB_RELEASES["demo-rel-mouserat"]["artist-credit"][0]["artist"]["id"]

    async def pictures():
        work = ArtistWork()
        try:
            found = {}
            for mbid in (wyld, satellites, mouse_rat):

                async def read(mbid: str = mbid):
                    return await work.musicbrainz.run(partial(demo.fetch_artist, mbid))

                _, found[mbid] = await artist_fill.fill(
                    mbid,
                    read,
                    lacks_biography=False,
                    lacks_picture=True,
                    asking=asking,
                    client=work.http,
                    deadline=asyncio.get_running_loop().time() + 5,
                )
            return found
        finally:
            await work.close()

    found = asyncio.run(pictures())
    assert found[wyld].picture is not None
    assert found[wyld].picture.name == "artist.png"
    assert found[wyld].picture.credit.source == "fanart.tv"
    assert found[satellites].picture is not None
    assert found[satellites].picture.name == "artist.jpg"
    assert found[satellites].picture.credit.source == "Wikimedia Commons"
    assert found[mouse_rat].picture is None and found[mouse_rat].picture_failed is None
