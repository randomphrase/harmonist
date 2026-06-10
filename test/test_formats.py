"""Format dispatch + per-format tag round-trip tests.

Parametrised over the available fixtures so adding a new format only
means dropping a `sine.<ext>` fixture and listing it in FIXTURES.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from harmonist import formats
from harmonist.tagger import tag_album

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# (extension, fixture filename). Each fixture is a ~1s sine tone.
# Ogg Vorbis (.ogg) is omitted: the local ffmpeg build can't encode it
# and oggenc isn't installed. Its code path is identical to Opus (both
# Ogg containers, both Vorbis comments via the shared _vorbis tagger),
# so Opus coverage is representative.
FIXTURES = [
    (".m4a", "sine.m4a"),
    (".mp3", "sine.mp3"),
    (".flac", "sine.flac"),
    (".opus", "sine.opus"),
]


def _release_one_track() -> dict:
    return {
        "id": "rel-fmt-1",
        "title": "Format Album",
        "status": "Official",
        "country": "GB",
        "date": "2022-03-04",
        "barcode": "5051234567890",
        "asin": "B00FMT0001",
        "artist-credit": [
            {"artist": {"id": "art-1", "name": "Format Artist"}, "name": "Format Artist"},
        ],
        "release-group": {"id": "rg-1", "primary-type": "Album"},
        "label-info-list": [
            {"label": {"name": "Test Label"}, "catalog-number": "CAT-9"},
        ],
        "medium-list": [
            {
                "position": "1",
                "format": "CD",
                "track-list": [
                    {
                        "id": "rt-1",
                        "position": "1",
                        "title": "The Track",
                        "recording": {"id": "rec-1", "title": "The Track"},
                    },
                ],
            },
        ],
    }


def _make_album(tmp_path: Path, fixture: str, name: str = "track") -> Path:
    d = tmp_path / "Artist" / "Album"
    d.mkdir(parents=True)
    ext = Path(fixture).suffix
    dst = d / f"01 {name}{ext}"
    shutil.copy(FIXTURES_DIR / fixture, dst)
    return d


# ---------- dispatch ----------


def test_supported_extensions_includes_known_formats():
    exts = formats.supported_extensions()
    for e in (".m4a", ".mp3", ".flac", ".ogg", ".opus"):
        assert e in exts


@pytest.mark.parametrize(("ext", "fixture"), FIXTURES)
def test_is_supported(ext, fixture):
    assert formats.is_supported(Path(f"x{ext}"))


def test_is_not_supported_for_unknown_ext(tmp_path):
    assert not formats.is_supported(tmp_path / "cover.jpg")
    assert not formats.is_supported(tmp_path / "notes.txt")


# ---------- round-trip per format ----------


@pytest.mark.parametrize(("ext", "fixture"), FIXTURES)
def test_tag_and_read_back(tmp_path, ext, fixture):
    d = _make_album(tmp_path, fixture)
    n = tag_album(d, _release_one_track())
    assert n == 1
    f = next(d.glob(f"*{ext}"))

    assert formats.read_album_id(f) == "rel-fmt-1"
    assert formats.read_album_title(f) == "Format Album"
    assert formats.read_artist(f) == "Format Artist"
    assert formats.read_track_title(f) == "The Track"
    # ~1s fixtures, within a wide tolerance for encoder padding
    dur = formats.read_duration_ms(f)
    assert dur is not None
    assert 900 <= dur <= 1200


@pytest.mark.parametrize(("ext", "fixture"), FIXTURES)
def test_read_scan_fields_matches_individual_reads(tmp_path, ext, fixture):
    """The single-open scan read returns the same values as the per-field
    reads (the consolidation must be behaviour-identical), plus the codec."""
    d = _make_album(tmp_path, fixture)
    tag_album(d, _release_one_track())
    f = next(d.glob(f"*{ext}"))

    sf = formats.read_scan_fields(f)
    assert sf.album_id == formats.read_album_id(f) == "rel-fmt-1"
    assert sf.album_title == formats.read_album_title(f) == "Format Album"
    assert sf.artist == formats.read_artist(f) == "Format Artist"
    assert sf.codec == formats.describe(f)


_TINY_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00" + b"\x00" * 40


@pytest.mark.parametrize(("ext", "fixture"), FIXTURES)
def test_read_cover_and_has_cover_after_embedding(tmp_path, ext, fixture):
    """Embedding cover art is detected by has_cover and extracted by
    read_cover (image bytes + mime), across all formats."""
    d = _make_album(tmp_path, fixture)
    cover = tmp_path / "art.jpg"
    cover.write_bytes(_TINY_JPEG)
    tag_album(d, _release_one_track(), cover_path=cover)
    f = next(d.glob(f"*{ext}"))

    assert formats.read_scan_fields(f).has_cover is True
    result = formats.read_cover(f)
    assert result is not None
    data, mime = result
    assert data == _TINY_JPEG
    assert mime == "image/jpeg"


@pytest.mark.parametrize(("ext", "fixture"), FIXTURES)
def test_read_cover_none_when_no_art(tmp_path, ext, fixture):
    d = _make_album(tmp_path, fixture)
    tag_album(d, _release_one_track())  # no cover embedded
    f = next(d.glob(f"*{ext}"))
    assert formats.read_scan_fields(f).has_cover is False
    assert formats.read_cover(f) is None


def test_read_scan_fields_untagged_has_codec_but_no_tags(tmp_path):
    d = _make_album(tmp_path, "sine.flac")
    f = next(d.glob("*.flac"))
    sf = formats.read_scan_fields(f)
    assert sf.album_id is None
    assert sf.album_title is None
    assert sf.artist is None
    assert sf.codec == "FLAC"


@pytest.mark.parametrize(("ext", "fixture"), FIXTURES)
def test_untagged_reads_return_none(tmp_path, ext, fixture):
    d = _make_album(tmp_path, fixture)
    f = next(d.glob(f"*{ext}"))
    # A fresh fixture has no MB Album Id
    assert formats.read_album_id(f) is None


@pytest.mark.parametrize(("ext", "fixture"), FIXTURES)
def test_comment_preserved_through_tagging(tmp_path, ext, fixture):
    """The comment field carries the Bandcamp-URL fallback and must
    survive a tag write."""
    d = _make_album(tmp_path, fixture)
    f = next(d.glob(f"*{ext}"))

    # Seed a comment using the per-format module directly.
    _seed_comment(f, "https://artist.bandcamp.com/album/x")
    tag_album(d, _release_one_track())
    assert formats.read_comment(f) == "https://artist.bandcamp.com/album/x"


def _seed_comment(path: Path, value: str) -> None:
    ext = path.suffix.lower()
    audio: Any
    if ext in (".m4a", ".mp4"):
        from mutagen.mp4 import MP4

        audio = MP4(path)
        audio["\xa9cmt"] = [value]
        audio.save()
    elif ext == ".mp3":
        from mutagen.id3 import COMM, Encoding
        from mutagen.mp3 import MP3

        audio = MP3(path)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.add(COMM(encoding=Encoding.UTF8, lang="eng", desc="", text=[value]))
        audio.save()
    elif ext == ".flac":
        from mutagen.flac import FLAC

        audio = FLAC(path)
        audio["COMMENT"] = [value]
        audio.save()
    elif ext == ".opus":
        from mutagen.oggopus import OggOpus

        audio = OggOpus(path)
        audio["COMMENT"] = [value]
        audio.save()
    else:
        raise AssertionError(f"no comment-seeder for {ext}")


# ---------- describe / format label ----------


@pytest.mark.parametrize(
    ("ext", "fixture", "expected"),
    [
        (".m4a", "sine.m4a", "ALAC"),  # the fixture is ALAC-encoded
        (".mp3", "sine.mp3", "MP3"),
        (".flac", "sine.flac", "FLAC"),
        (".opus", "sine.opus", "Opus"),
    ],
)
def test_describe_label(tmp_path, ext, fixture, expected):
    d = _make_album(tmp_path, fixture)
    f = next(d.glob(f"*{ext}"))
    assert formats.describe(f) == expected


def test_describe_none_for_unknown(tmp_path):
    assert formats.describe(tmp_path / "cover.jpg") is None


# ---------- scanner integration ----------


def test_scanner_picks_up_mp3_album(tmp_path):
    from harmonist.models import AlbumState
    from harmonist.scanner import scan

    d = _make_album(tmp_path, "sine.mp3")
    albums = scan(tmp_path)
    assert len(albums) == 1
    assert albums[0].path == d
    assert albums[0].track_count == 1
    assert albums[0].state == AlbumState.NEW  # no sidecar yet
    assert albums[0].audio_format == "MP3"


def test_scanner_audio_format_single(tmp_path):
    from harmonist.scanner import scan

    _make_album(tmp_path, "sine.flac")
    assert scan(tmp_path)[0].audio_format == "FLAC"


def test_scanner_audio_format_mixed(tmp_path):
    """A dir with files of differing formats reports 'Mixed'."""
    from harmonist.scanner import scan

    d = _make_album(tmp_path, "sine.flac", name="a")
    shutil.copy(FIXTURES_DIR / "sine.mp3", d / "02 b.mp3")
    assert scan(tmp_path)[0].audio_format == "Mixed"
