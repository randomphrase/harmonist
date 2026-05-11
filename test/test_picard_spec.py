"""Picard MP4 tag-mapping spec conformance.

Verifies that `PicardCompatibleTagger.tag_album` writes atoms whose NAMES
and VALUES match the documented Picard mapping
(https://picard.musicbrainz.org/docs/mappings/#mp4) for the subset of
fields we currently support.

This is a *spec* conformance test, not a byte-diff against actual Picard
output — that would require running real Picard against a reference
release and committing its output. (Roadmap: see KNOWN_GAPS at the
bottom of this file.)
"""
from __future__ import annotations

import shutil
from pathlib import Path

from mutagen.mp4 import MP4

from harmonist.tagger import (
    ATOM_ASIN,
    ATOM_BARCODE,
    ATOM_CATALOG,
    ATOM_LABEL,
    ATOM_MB_ALBUM_ARTIST_ID,
    ATOM_MB_ALBUM_COUNTRY,
    ATOM_MB_ALBUM_ID,
    ATOM_MB_ALBUM_STATUS,
    ATOM_MB_ALBUM_TYPE,
    ATOM_MB_ARTIST_ID,
    ATOM_MB_RELEASE_GROUP_ID,
    ATOM_MB_RELEASE_TRACK_ID,
    ATOM_MB_TRACK_ID,
    ATOM_MEDIA,
    PicardCompatibleTagger,
)


FIXTURES_DIR = Path(__file__).parent / "fixtures"
SINE_M4A = FIXTURES_DIR / "sine.m4a"


def _fully_populated_release() -> dict:
    """An MB release dict with every field Picard's spec touches.

    Single-disc, 2-track. Each track has its own artist credit (different
    from the release-level credit) so we can verify the per-track artist
    atoms override the album-level ones.
    """
    return {
        "id": "11111111-2222-3333-4444-555555555555",
        "title": "Reference Album",
        "status": "Official",
        "country": "GB",
        "date": "1992-08-17",
        "barcode": "5051083012345",
        "asin": "B00REFERNC",
        "artist-credit": [
            {
                "artist": {
                    "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "name": "Reference Artist",
                },
                "name": "Reference Artist",
            },
        ],
        "release-group": {
            "id": "abcdef01-2345-6789-abcd-ef0123456789",
            "primary-type": "Album",
        },
        "label-info-list": [
            {
                "label": {"name": "Reference Records"},
                "catalog-number": "REF-001",
            },
        ],
        "medium-list": [
            {
                "position": "1",
                "format": "Digital Media",
                "track-list": [
                    {
                        "id": "track1111-1111-1111-1111-111111111111",
                        "position": "1",
                        "title": "Side A Track",
                        "recording": {
                            "id": "rec01111-1111-1111-1111-111111111111",
                            "title": "Side A Track",
                            "length": "180000",
                        },
                    },
                    {
                        "id": "track2222-2222-2222-2222-222222222222",
                        "position": "2",
                        "title": "Side B Track",
                        "artist-credit": [
                            {
                                "artist": {
                                    "id": "guest1111-1111-1111-1111-111111111111",
                                    "name": "Guest",
                                },
                                "name": "Guest",
                                "joinphrase": " feat. ",
                            },
                            {
                                "artist": {
                                    "id": "host11111-1111-1111-1111-111111111111",
                                    "name": "Host",
                                },
                                "name": "Host",
                            },
                        ],
                        "recording": {
                            "id": "rec02222-2222-2222-2222-222222222222",
                            "title": "Side B Track",
                            "length": "240000",
                        },
                    },
                ],
            },
        ],
    }


def _setup_album(tmp_path: Path, n_tracks: int) -> Path:
    d = tmp_path / "Reference Artist" / "Reference Album"
    d.mkdir(parents=True)
    for i in range(1, n_tracks + 1):
        shutil.copy(SINE_M4A, d / f"{i:02d} Track {i}.m4a")
    return d


def _atom_str(audio: MP4, atom: str) -> str:
    return audio[atom][0].decode("utf-8")


def _atom_strs(audio: MP4, atom: str) -> list[str]:
    return [v.decode("utf-8") for v in audio[atom]]


# ---------- album-level atoms ----------

def test_album_level_mbid_atoms_match_picard_names(tmp_path):
    """Album-level MB MBIDs use Picard's exact ----:com.apple.iTunes:<Spaced> form."""
    album_dir = _setup_album(tmp_path, 2)
    PicardCompatibleTagger().tag_album(album_dir, _fully_populated_release())

    audio = MP4(album_dir / "01 Track 1.m4a")
    # Picard spec: album-level MBIDs every track carries
    assert _atom_str(audio, ATOM_MB_ALBUM_ID) == "11111111-2222-3333-4444-555555555555"
    assert _atom_strs(audio, ATOM_MB_ALBUM_ARTIST_ID) == ["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"]
    assert _atom_str(audio, ATOM_MB_RELEASE_GROUP_ID) == "abcdef01-2345-6789-abcd-ef0123456789"
    assert _atom_str(audio, ATOM_MB_ALBUM_TYPE) == "Album"
    assert _atom_str(audio, ATOM_MB_ALBUM_STATUS) == "Official"
    assert _atom_str(audio, ATOM_MB_ALBUM_COUNTRY) == "GB"


def test_album_level_optional_metadata_atoms(tmp_path):
    """Label / catalog / barcode / asin / media — Picard writes these when MB has them."""
    album_dir = _setup_album(tmp_path, 2)
    PicardCompatibleTagger().tag_album(album_dir, _fully_populated_release())

    audio = MP4(album_dir / "01 Track 1.m4a")
    assert _atom_str(audio, ATOM_LABEL) == "Reference Records"
    assert _atom_str(audio, ATOM_CATALOG) == "REF-001"
    assert _atom_str(audio, ATOM_BARCODE) == "5051083012345"
    assert _atom_str(audio, ATOM_ASIN) == "B00REFERNC"
    assert _atom_str(audio, ATOM_MEDIA) == "Digital Media"


def test_album_level_standard_text_tags(tmp_path):
    """Picard maps album/albumartist/date to ©alb/aART/©day."""
    album_dir = _setup_album(tmp_path, 2)
    PicardCompatibleTagger().tag_album(album_dir, _fully_populated_release())

    audio = MP4(album_dir / "01 Track 1.m4a")
    assert audio["\xa9alb"] == ["Reference Album"]
    assert audio["aART"] == ["Reference Artist"]
    assert audio["\xa9day"] == ["1992-08-17"]


# ---------- per-track atoms ----------

def test_per_track_mbid_atoms_use_picard_convention(tmp_path):
    """
    Picard's MP4 mapping is subtle:
      MusicBrainz Track Id          ←  recording.id   (the RECORDING MBID)
      MusicBrainz Release Track Id  ←  track.id       (the release-track MBID)
      MusicBrainz Artist Id         ←  per-track artist credit MBIDs
    """
    album_dir = _setup_album(tmp_path, 2)
    PicardCompatibleTagger().tag_album(album_dir, _fully_populated_release())

    t1 = MP4(album_dir / "01 Track 1.m4a")
    assert _atom_str(t1, ATOM_MB_TRACK_ID) == "rec01111-1111-1111-1111-111111111111"
    assert _atom_str(t1, ATOM_MB_RELEASE_TRACK_ID) == "track1111-1111-1111-1111-111111111111"
    # Track 1 has no track-level artist credit → falls back to release credit
    assert _atom_strs(t1, ATOM_MB_ARTIST_ID) == ["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"]

    t2 = MP4(album_dir / "02 Track 2.m4a")
    assert _atom_str(t2, ATOM_MB_TRACK_ID) == "rec02222-2222-2222-2222-222222222222"
    assert _atom_str(t2, ATOM_MB_RELEASE_TRACK_ID) == "track2222-2222-2222-2222-222222222222"
    # Track 2 HAS its own artist credit → uses guest + host MBIDs
    assert _atom_strs(t2, ATOM_MB_ARTIST_ID) == [
        "guest1111-1111-1111-1111-111111111111",
        "host11111-1111-1111-1111-111111111111",
    ]


def test_per_track_standard_text_tags(tmp_path):
    album_dir = _setup_album(tmp_path, 2)
    PicardCompatibleTagger().tag_album(album_dir, _fully_populated_release())

    t1 = MP4(album_dir / "01 Track 1.m4a")
    assert t1["\xa9nam"] == ["Side A Track"]
    assert t1["\xa9ART"] == ["Reference Artist"]
    assert t1["trkn"] == [(1, 2)]

    t2 = MP4(album_dir / "02 Track 2.m4a")
    assert t2["\xa9nam"] == ["Side B Track"]
    # Per-track artist credit phrase, with joinphrase
    assert t2["\xa9ART"] == ["Guest feat. Host"]
    assert t2["trkn"] == [(2, 2)]


def test_disc_number_atom(tmp_path):
    """Picard writes disk = (disc_pos, disc_total) on every track."""
    album_dir = _setup_album(tmp_path, 2)
    PicardCompatibleTagger().tag_album(album_dir, _fully_populated_release())

    audio = MP4(album_dir / "01 Track 1.m4a")
    assert audio["disk"] == [(1, 1)]


# ---------- exhaustive atom inventory ----------

EXPECTED_PICARD_ATOMS_WE_WRITE = {
    # Album-level MBIDs
    ATOM_MB_ALBUM_ID,
    ATOM_MB_ALBUM_ARTIST_ID,
    ATOM_MB_RELEASE_GROUP_ID,
    ATOM_MB_ALBUM_TYPE,
    ATOM_MB_ALBUM_STATUS,
    ATOM_MB_ALBUM_COUNTRY,
    # Per-track MBIDs
    ATOM_MB_TRACK_ID,
    ATOM_MB_RELEASE_TRACK_ID,
    ATOM_MB_ARTIST_ID,
    # Album metadata
    ATOM_LABEL,
    ATOM_CATALOG,
    ATOM_BARCODE,
    ATOM_ASIN,
    ATOM_MEDIA,
    # Standard text tags
    "\xa9alb", "\xa9nam", "\xa9ART", "aART", "\xa9day",
    "trkn", "disk",
}


def test_complete_inventory_against_picard_spec(tmp_path):
    """
    Verifies the FULL set of atoms a tagged file ends up with, given a
    fully-populated MB release. Detects accidental atom additions/removals.
    """
    album_dir = _setup_album(tmp_path, 2)
    PicardCompatibleTagger().tag_album(album_dir, _fully_populated_release())

    audio = MP4(album_dir / "01 Track 1.m4a")
    actual = set(audio.keys())

    # Every atom we expect must be present
    missing = EXPECTED_PICARD_ATOMS_WE_WRITE - actual
    assert not missing, f"missing Picard atoms: {missing}"

    # And no atom we DIDN'T expect should be present (catches accidental writes).
    # Allow legacy ----:com.apple.iTunes:MUSICBRAINZ_RELEASEID to be absent.
    extra = actual - EXPECTED_PICARD_ATOMS_WE_WRITE
    # mutagen may surface bare format atoms (©too = encoded by …) that come
    # from the fixture file itself, not from our tagger. Filter those out.
    extra = {a for a in extra if not a.startswith("\xa9too")}
    assert not extra, f"unexpected atoms written: {extra}"


# ---------- known gaps ----------
#
# Picard atoms we DO NOT yet write (documented future work). Source: the
# Picard spec at https://picard.musicbrainz.org/docs/mappings/#mp4. Tests
# below assert these are NOT yet present — flipping any of them to "should
# be present" will fail clearly, signalling that this gap list needs an
# update and the tagger's coverage needs to expand.

KNOWN_GAPS = {
    "----:com.apple.iTunes:SCRIPT",
    "----:com.apple.iTunes:LANGUAGE",
    "----:com.apple.iTunes:ORIGINALDATE",
    "----:com.apple.iTunes:ORIGINALYEAR",
    "----:com.apple.iTunes:ISRC",
    "----:com.apple.iTunes:DISCSUBTITLE",
    "----:com.apple.iTunes:WORK",
    "----:com.apple.iTunes:MOVEMENT",
    "\xa9wrt",  # composer
    "\xa9gen",  # genre (we have the atom name but don't populate it from MB)
}


def test_known_gaps_not_yet_written(tmp_path):
    """Sanity-check on what we DON'T cover yet — fails loudly if a future
    change starts writing one of these (without updating the gap list).
    """
    album_dir = _setup_album(tmp_path, 2)
    PicardCompatibleTagger().tag_album(album_dir, _fully_populated_release())

    audio = MP4(album_dir / "01 Track 1.m4a")
    actually_written = set(audio.keys())
    for gap in KNOWN_GAPS:
        assert gap not in actually_written, (
            f"{gap!r} is now being written — update KNOWN_GAPS list "
            f"and add a positive assertion in test_complete_inventory_against_picard_spec"
        )
