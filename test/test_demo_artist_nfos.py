"""The public demo exercises the real artist inventory and exclusion rules."""

import pytest

from harmonist import artist_nfo, artists, demo, mb_lookup, nfo_writer, scanner
from harmonist.models import AlbumState

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
