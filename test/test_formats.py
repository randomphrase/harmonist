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
        "text-representation": {"language": "eng", "script": "Latn"},
        "artist-credit": [
            {
                "artist": {
                    "id": "art-1",
                    "name": "Format Artist",
                    "sort-name": "Format Artist, The",
                },
                "name": "Format Artist",
            },
        ],
        "release-group": {
            "id": "rg-1",
            "primary-type": "Album",
            "first-release-date": "2018-07-09",
        },
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
                        "recording": {
                            "id": "rec-1",
                            "title": "The Track",
                            "isrc-list": ["GBFMT2100001"],
                        },
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


@pytest.mark.parametrize(("ext", "fixture"), FIXTURES)
def test_sort_artists_original_date_script_written(tmp_path, ext, fixture):
    """The Picard sort/artists/original-date/script tags round-trip in each
    format's native representation."""
    d = _make_album(tmp_path, fixture)
    tag_album(d, _release_one_track())
    f = next(d.glob(f"*{ext}"))
    sort, artists, orig_date, orig_year, script = _read_new_tags(f)

    assert sort == "Format Artist, The"
    assert artists == ["Format Artist"]
    assert orig_date == "2018-07-09"
    assert orig_year == "2018"
    assert script == "Latn"


def _first(values: list[str]) -> str | None:
    return values[0] if values else None


@pytest.mark.parametrize(("ext", "fixture"), FIXTURES)
def test_isrc_written(tmp_path, ext, fixture):
    """The track recording's ISRC round-trips in each format's native tag."""
    d = _make_album(tmp_path, fixture)
    tag_album(d, _release_one_track())
    f = next(d.glob(f"*{ext}"))
    assert _read_isrcs(f) == ["GBFMT2100001"]


def _read_isrcs(path: Path) -> list[str]:
    ext = path.suffix.lower()
    if ext in (".m4a", ".mp4"):
        from mutagen.mp4 import MP4

        a = MP4(path)
        return [b.decode("utf-8") for b in a.get("----:com.apple.iTunes:ISRC", [])]
    if ext == ".mp3":
        from mutagen.mp3 import MP3

        frame = MP3(path).tags.get("TSRC")
        return [str(t) for t in frame.text] if frame else []
    from mutagen import File as MutagenFile

    tags = MutagenFile(path).tags
    return [str(v) for v in (tags.get("ISRC") or [])]


def _read_new_tags(path: Path) -> tuple[str | None, list[str], str | None, str | None, str | None]:
    """Read (artistsort, artists, originaldate, originalyear, script) using the
    native tag layer for the file's format."""
    ext = path.suffix.lower()
    if ext in (".m4a", ".mp4"):
        from mutagen.mp4 import MP4

        a = MP4(path)
        pre = "----:com.apple.iTunes:"

        def ff(name: str) -> str | None:
            v = a.get(f"{pre}{name}")
            return v[0].decode("utf-8") if v else None

        return (
            _first([str(v) for v in a.get("soar", [])]),
            [b.decode("utf-8") for b in a.get(f"{pre}ARTISTS", [])],
            ff("ORIGINALDATE"),
            ff("ORIGINALYEAR"),
            ff("SCRIPT"),
        )
    if ext == ".mp3":
        from mutagen.mp3 import MP3

        tags = MP3(path).tags

        def txxx(desc: str) -> list[str]:
            frame = tags.get(f"TXXX:{desc}")
            return [str(t) for t in frame.text] if frame else []

        def text(fid: str) -> str | None:
            frame = tags.get(fid)
            return str(frame.text[0]) if frame and frame.text else None

        orig = text("TDOR")
        return (
            text("TSOP"),
            txxx("ARTISTS"),
            orig,
            (orig[:4] if orig else None),
            _first(txxx("SCRIPT")),
        )
    # Vorbis-comment formats (.flac, .opus, .ogg)
    from mutagen import File as MutagenFile

    tags = MutagenFile(path).tags

    def vc(key: str) -> list[str]:
        return [str(v) for v in (tags.get(key) or [])]

    return (
        _first(vc("ARTISTSORT")),
        vc("ARTISTS"),
        _first(vc("ORIGINALDATE")),
        _first(vc("ORIGINALYEAR")),
        _first(vc("SCRIPT")),
    )


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


def test_scanner_sets_has_tag_mbid(tmp_path):
    """has_tag_mbid reflects whether the tracks carry an MB Album Id atom —
    the signal the inbox uses to decide an orphan is reconcilable."""
    from harmonist.scanner import scan

    d = _make_album(tmp_path, "sine.m4a")
    assert scan(tmp_path)[0].has_tag_mbid is False  # fresh fixture: no MBID atom

    tag_album(d, _release_one_track())  # writes the MB Album Id atom
    assert scan(tmp_path)[0].has_tag_mbid is True


def test_scanner_audio_format_mixed(tmp_path):
    """A dir with files of differing formats reports 'Mixed'."""
    from harmonist.scanner import scan

    d = _make_album(tmp_path, "sine.flac", name="a")
    shutil.copy(FIXTURES_DIR / "sine.mp3", d / "02 b.mp3")
    assert scan(tmp_path)[0].audio_format == "Mixed"
