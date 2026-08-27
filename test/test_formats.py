"""Format dispatch + per-format tag round-trip tests.

Parametrised over the available fixtures so adding a new format only
means dropping a `sine.<ext>` fixture and listing it in FIXTURES.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from mutagen import MutagenError
from mutagen.mp4 import MP4

from harmonist import formats
from harmonist.formats import m4a
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


# ---------- the read side of the comparison (#106) ----------


@pytest.mark.parametrize(("ext", "fixture"), FIXTURES)
def test_read_tags_recovers_what_the_tagger_wrote(tmp_path, ext, fixture):
    """The real test of the read side: tag a file from a MusicBrainz release,
    read it back, and check every field survived the round trip.

    Each format stores these somewhere different — MP4 freeform `----` atoms,
    ID3 `TPUB` and `TXXX` frames, Vorbis comments — so a mapping that's wrong in
    one direction only shows up here. Comparing against MusicBrainz with a
    field Harmonist can't read back would report a difference that isn't real.
    """
    d = _make_album(tmp_path, fixture)
    tag_album(d, _release_one_track())
    f = next(d.glob(f"*{ext}"))

    t = formats.read_tags(f)
    assert t.unreadable is False
    assert t.album == "Format Album"
    assert t.album_artist == "Format Artist"
    assert t.artist == "Format Artist"
    assert t.title == "The Track"
    assert t.date == "2022-03-04"
    assert t.label == "Test Label"
    assert t.catalog_number == "CAT-9"
    assert t.barcode == "5051234567890"
    assert t.track_num == 1
    assert t.disc_num == 1
    assert t.duration_ms is not None and 900 <= t.duration_ms <= 1200


@pytest.mark.parametrize(("ext", "fixture"), FIXTURES)
def test_read_tags_reports_absent_fields_as_none_not_empty_string(tmp_path, ext, fixture):
    """An untagged file must come back with None, not "" — the comparison
    treats None as "absent" and would render an empty string as a value that
    differs from MusicBrainz."""
    d = _make_album(tmp_path, fixture)
    t = formats.read_tags(next(d.glob(f"*{ext}")))

    assert t.unreadable is False  # readable, just untagged
    for name in ("album", "album_artist", "artist", "title", "label", "catalog_number"):
        assert getattr(t, name) is None, f"{name} came back {getattr(t, name)!r}"


@pytest.mark.parametrize(("ext", "fixture"), FIXTURES)
def test_read_tags_flags_a_file_it_cannot_open(tmp_path, ext, fixture):
    """#112's distinction, at the field-read layer this time."""
    d = _make_album(tmp_path, fixture)
    f = next(d.glob(f"*{ext}"))
    f.write_bytes(b"not audio at all")

    t = formats.read_tags(f)
    assert t.unreadable is True
    assert t.album is None  # …and still looks empty, which is the trap


@pytest.mark.parametrize(("ext", "fixture"), FIXTURES)
def test_a_track_numbered_by_vinyl_side_reads_as_no_number(tmp_path, ext, fixture):
    """#137: `TRACKNUMBER=A1` used to raise ValueError out of `read_tags`, which
    took the whole album page down with it — on exactly the hand-numbered vinyl
    rips an adopted library is full of.

    None is the honest answer: the file carries no number Harmonist can use, so
    the tracklist falls back to file order for it (#135). Every format has to
    agree on that, which is why this is parametrised rather than a Vorbis test.
    """
    d = _make_album(tmp_path, fixture)
    tag_album(d, _release_one_track())
    f = next(d.glob(f"*{ext}"))
    _set_raw_track_number(f, "A1")

    t = formats.read_tags(f)
    assert t.unreadable is False  # the file is perfectly fine
    assert t.track_num is None
    assert t.title == "The Track"  # …and the rest of the tags still read


def _set_raw_track_number(path: Path, value: str) -> None:
    """Write a non-numeric track number the way a vinyl rip really carries it.

    Goes through each format's native API rather than `write_tags`, which takes
    an int — the whole point is a value Harmonist itself would never write but
    has to survive reading.
    """
    if path.suffix == ".mp3":
        from mutagen.id3 import ID3, TRCK, Encoding

        tags = ID3(path)
        tags.setall("TRCK", [TRCK(encoding=Encoding.UTF8, text=[value])])
        tags.save(path)
    elif path.suffix == ".m4a":
        # MP4 `trkn` is a binary (track, total) pair — it cannot hold "A1" at
        # all, so the bad-input case simply doesn't exist for this format.
        pytest.skip("MP4 track numbers are integers by construction")
    else:
        from mutagen import File as MutagenFile

        audio = MutagenFile(path)
        audio["TRACKNUMBER"] = [value]
        audio.save()


def test_read_tags_on_an_unsupported_extension_is_empty_not_unreadable(tmp_path):
    """Nothing went wrong — there's simply nothing to read. Flagging it would
    put a "couldn't read this" notice on a stray text file."""
    p = tmp_path / "notes.txt"
    p.write_text("sleeve notes")
    t = formats.read_tags(p)
    assert t.unreadable is False
    assert t.album is None


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


def test_describe_names_aac_rather_than_its_container(tmp_path):
    """#254: `MP4Info.codec` is an RFC 6381 string — "mp4a.40.2", not "mp4a" —
    so an equality test against the bare four-character code never matched and
    every AAC file read as "MP4".

    The point of the label is to separate lossless ALAC from lossy AAC, and
    "MP4" is the answer that should be reserved for a container whose codec
    can't be pinned down.
    """
    from mutagen.mp4 import MP4

    dst = tmp_path / "01 track.m4a"
    shutil.copy(FIXTURES_DIR / "sine-aac.m4a", dst)
    assert MP4(dst).info.codec == "mp4a.40.2"  # what the label has to cope with

    assert formats.describe(dst) == "AAC"
    assert formats.read_scan_fields(dst).codec == "AAC"


def test_describe_none_for_unknown(tmp_path):
    assert formats.describe(tmp_path / "cover.jpg") is None


# ---------- stream quality (#130) ----------


@pytest.mark.parametrize(
    ("ext", "fixture", "expected"),
    [
        # Lossless: sample rate and bit depth, and NO bitrate — a lossless
        # bitrate is an artifact of how well the audio compressed, not a
        # quality the user chose.
        (".m4a", "sine.m4a", "44.1 kHz · 16 bit"),
        (".flac", "sine.flac", "44.1 kHz · 16 bit"),
        # Lossy: bitrate instead of depth, and MP3 alone names its mode.
        (".mp3", "sine.mp3", "44.1 kHz · 128 kbps CBR"),
        # Opus records no sample rate at all — it always decodes at 48 kHz
        # whatever went in — so the clause is absent, not "unknown".
        (".opus", "sine.opus", "75 kbps"),
    ],
)
def test_quality_label_per_format(tmp_path, ext, fixture, expected):
    d = _make_album(tmp_path, fixture)
    f = next(d.glob(f"*{ext}"))
    assert formats.read_scan_fields(f).quality.label == expected


def test_a_lossy_stream_reports_no_bit_depth_even_when_mutagen_offers_one(tmp_path):
    """MP4 is the one container here holding both a lossless and a lossy codec,
    and `MP4Info` reports `bits_per_sample` for BOTH — 16, for AAC.

    That 16 describes the decoder's output, not the file: an AAC download
    labelled "16 bit" claims a fidelity it doesn't have. The AAC fixture is
    what makes this a real check — every other lossy format simply has no such
    attribute, so a reader that dropped the lossless guard would still look
    correct on them.
    """
    d = tmp_path / "aac"
    d.mkdir()
    dst = d / "01 track.m4a"
    shutil.copy(FIXTURES_DIR / "sine-aac.m4a", dst)

    from mutagen.mp4 import MP4

    assert MP4(dst).info.bits_per_sample == 16  # what a reader must NOT repeat

    quality = formats.read_scan_fields(dst).quality
    assert quality.bit_depth is None
    assert quality.label == "44.1 kHz · 116 kbps"


def test_a_lossless_stream_reports_depth_and_no_bitrate(tmp_path):
    """The other half of the pair: ALAC does carry a real bit depth, and its
    bitrate is an artifact of how well the audio compressed rather than a
    quality anyone chose — so it isn't reported."""
    d = _make_album(tmp_path, "sine.m4a")
    quality = formats.read_scan_fields(next(d.glob("*.m4a"))).quality
    assert quality.bit_depth == 16
    assert quality.bitrate is None


def test_hi_res_rate_drops_a_trailing_zero(tmp_path):
    """48000 Hz reads as "48 kHz", not "48.0 kHz" — and 24-bit depth survives
    the read, which is the difference the Format row exists to show."""
    d = _make_album(tmp_path, "sine-hires.flac")
    f = next(d.glob("*.flac"))
    assert formats.read_scan_fields(f).quality.label == "48 kHz · 24 bit"


def test_a_file_with_no_comment_block_still_reports_its_quality(tmp_path):
    """A FLAC carrying no VORBIS_COMMENT block at all still has a perfectly
    readable stream — what a file IS doesn't depend on whether anyone tagged it.

    The block has to be REMOVED, not emptied: mutagen hands back an empty
    `VCFLACDict` for a file that merely has no values, and only a file missing
    the block outright reaches the reader's untagged early-return. An empty
    fixture leaves that branch unvisited and the test unable to fail.
    """
    from mutagen.flac import FLAC, VCFLACDict

    d = _make_album(tmp_path, "sine.flac")
    f = next(d.glob("*.flac"))
    audio = FLAC(f)
    audio.metadata_blocks[:] = [b for b in audio.metadata_blocks if not isinstance(b, VCFLACDict)]
    audio.tags = None
    audio.save()
    assert FLAC(f).tags is None  # the branch under test is genuinely reachable

    sf = formats.read_scan_fields(f)
    assert sf.album_title is None  # nothing to read
    assert sf.quality.label == "44.1 kHz · 16 bit"  # but the stream still speaks


@pytest.mark.parametrize(("ext", "fixture"), FIXTURES)
def test_an_unopenable_file_reports_no_quality(tmp_path, ext, fixture):
    d = _make_album(tmp_path, fixture)
    f = next(d.glob(f"*{ext}"))
    f.write_bytes(b"not audio at all")
    sf = formats.read_scan_fields(f)
    assert sf.unreadable is True
    assert sf.quality.label is None


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


def test_scanner_audio_quality_when_the_files_agree(tmp_path):
    from harmonist.scanner import scan

    d = _make_album(tmp_path, "sine.flac", name="a")
    shutil.copy(FIXTURES_DIR / "sine.flac", d / "02 b.flac")
    album = scan(tmp_path)[0]
    assert album.audio_format == "FLAC"
    assert album.audio_quality == "44.1 kHz · 16 bit"


def test_scanner_audio_quality_names_the_odd_tracks(tmp_path):
    """Half the album at a different bit depth is worth knowing about, so the
    row reports what most tracks say and how many don't — rather than
    collapsing to "Mixed" and throwing the answer away."""
    from harmonist.scanner import scan

    d = _make_album(tmp_path, "sine.flac", name="a")
    shutil.copy(FIXTURES_DIR / "sine.flac", d / "02 b.flac")
    shutil.copy(FIXTURES_DIR / "sine-hires.flac", d / "03 c.flac")
    album = scan(tmp_path)[0]
    assert album.audio_format == "FLAC"  # one codec throughout
    assert album.audio_quality == "44.1 kHz · 16 bit · 1 track differs"


def test_scanner_audio_quality_ignores_a_codec_difference_the_audio_doesnt_share(tmp_path):
    """An ALAC track beside a FLAC one is a mixed CODEC, which the label
    already says. Both are 44.1/16, so the audio is uniform and reporting a
    second disagreement for the same fact would overstate it."""
    from harmonist.scanner import scan

    d = _make_album(tmp_path, "sine.flac", name="a")
    shutil.copy(FIXTURES_DIR / "sine.m4a", d / "02 b.m4a")
    album = scan(tmp_path)[0]
    assert album.audio_format == "Mixed"
    assert album.audio_quality == "44.1 kHz · 16 bit"


def test_scanner_audio_quality_absent_when_nothing_could_be_read(tmp_path):
    from harmonist.scanner import scan

    d = _make_album(tmp_path, "sine.flac")
    next(d.glob("*.flac")).write_bytes(b"not audio at all")
    assert scan(tmp_path)[0].audio_quality is None


# ---------- the owned-tag set (#149) ----------


def _copy_fixture(tmp_path: Path, fixture: str) -> Path:
    dst = tmp_path / fixture
    shutil.copy(FIXTURES_DIR / fixture, dst)
    return dst


def _tagset(**overrides: Any) -> Any:
    """A minimal valid TagSet, plus whatever the test wants to vary."""
    from harmonist.formats.types import TagSet

    base: dict[str, Any] = {
        "mb_album_id": "album-mbid",
        "album": "Album",
        "album_artist": "Album Artist",
        "title": "Title",
        "artist": "Artist",
        "track_num": 1,
        "track_total": 1,
    }
    return TagSet(**{**base, **overrides})


def test_owned_fields_match_tagset_exactly():
    """`Owned` and `TagSet` name the same set of fields.

    The guard against silent drift in both directions: a new TagSet field that
    nobody classified would be written and never cleared (the #149 bug, back
    again for one field), and an Owned member with no TagSet field behind it
    would clear a tag Harmonist never writes — destroying user data.
    """
    from dataclasses import fields as dc_fields

    from harmonist.formats.owned import Owned
    from harmonist.formats.types import TagSet

    assert {f.name for f in dc_fields(TagSet)} == {f.value for f in Owned}


def test_every_owned_field_has_a_scope():
    from harmonist.formats.owned import ALBUM_FIELDS, SCOPE, TRACK_FIELDS, Owned

    assert set(SCOPE) == set(Owned)
    assert set(ALBUM_FIELDS) | set(TRACK_FIELDS) == set(Owned)
    assert not set(ALBUM_FIELDS) & set(TRACK_FIELDS)


def test_every_owned_field_has_a_significance():
    """A field added to `Owned` cannot default into applying itself unattended.

    This is the guard, and it is the whole reason the map lives beside `SCOPE`
    rather than in the gardener that reads it: adding a field to `Owned` without
    classifying it fails here, at the point of adding, rather than silently
    inheriting whatever the classifier does with an unknown key. The cost of
    forgetting `SCOPE` is a mis-rendered history row; the cost of forgetting this
    one is an unattended write nobody authorised (#267).

    Keyed over the whole diff vocabulary, not just `Owned`: `ARTWORK` is a key a
    plan really produces, so leaving it out would be a hole exactly where the map
    is supposed to be total.
    """
    from harmonist.formats.owned import ARTWORK, BY_VALUE, SIGNIFICANCE, Owned

    assert set(SIGNIFICANCE) == {f.value for f in Owned} | {ARTWORK}
    assert BY_VALUE <= set(Owned)


def test_a_value_sensitive_field_is_declared_at_its_higher_significance():
    """The runtime adjustment only ever lowers.

    A `BY_VALUE` field is declared IDENTITY and *lowered* to COSMETIC when the
    values turn out to differ trivially. Declared the other way round, the rule
    failing to fire would understate a real retitle as a spacing fix — and once
    #273 lets a level be trusted, understating is what writes something nobody
    agreed to. Overstating only ever costs a glance.
    """
    from harmonist.formats.owned import BY_VALUE, SIGNIFICANCE, Significance

    assert BY_VALUE
    for field in BY_VALUE:
        assert SIGNIFICANCE[field] is Significance.IDENTITY


def test_cosmetic_is_never_declared_only_derived():
    """COSMETIC describes a pair of values, not a field.

    No field is inherently cosmetic — `title` is only cosmetic on the occasions
    its two values differ by whitespace or casing. A field declared COSMETIC in
    the table would be one claiming that of every change it could ever carry.
    """
    from harmonist.formats.owned import SIGNIFICANCE, Significance

    assert Significance.COSMETIC not in SIGNIFICANCE.values()


def test_no_level_applies_itself_yet():
    """Every change goes to review, whatever its significance.

    Deliberate, and the reason it is asserted rather than merely true: nothing
    has watched this classification run against a real library yet, so the way to
    find out whether the table is right is to see its verdicts arrive in the
    Inbox. Starting closed also means the first cut of #32's runner cannot write
    anything unattended, whatever else is wrong with it.

    #273 turns `AUTO_APPLY` into a setting. Until it does, a member appearing
    here is a decision about somebody's files, so it should be the point of the
    change that adds it rather than a default that drifted.
    """
    from harmonist.formats.owned import AUTO_APPLY, Significance, needs_review

    assert AUTO_APPLY == frozenset()
    for level in Significance:
        assert needs_review(level), level


def test_no_identifier_is_classified_below_identity():
    """Every MusicBrainz id is IDENTITY, including ones that only move on a merge.

    Stated as a rule rather than left implicit in a list of thirty entries: an id
    changing is the album being re-pointed at something else, and the one case
    where that is settled rather than proposed — a merged release, which arrives
    as a redirect naming both ids — is corrected at the fetch and never reaches
    the classifier as a question (#268, `docs/design.md` §5).
    """
    from harmonist.formats.owned import SIGNIFICANCE, Owned, Significance

    # By suffix, not by the `mb_` prefix: `mb_album_status` and
    # `mb_album_country` are MusicBrainz's words for a release, not identifiers,
    # and they ARE enrichment. The count is pinned so a filter that silently
    # stopped selecting anything would fail here rather than pass vacuously.
    ids = [f for f in Owned if f.value.endswith(("_id", "_ids"))]
    assert len(ids) == 6
    for field in ids:
        assert SIGNIFICANCE[field] is Significance.IDENTITY, field


@pytest.mark.parametrize(
    ("module_name", "table"),
    [
        ("m4a", "OWNED_ATOMS"),
        ("mp3", "OWNED_FRAMES"),
        ("_vorbis", "OWNED_KEYS"),
    ],
)
def test_every_backend_maps_every_owned_field(module_name: str, table: str):
    """A backend that forgets a field stops clearing it — which is exactly the
    bug this set exists to prevent, reintroduced one format at a time."""
    import importlib

    from harmonist.formats.owned import Owned

    mod = importlib.import_module(f"harmonist.formats.{module_name}")
    assert set(getattr(mod, table)) == set(Owned)


@pytest.mark.parametrize(("ext", "fixture"), FIXTURES)
def test_retag_removes_owned_tags_the_new_release_lacks(tmp_path, ext, fixture):
    """The #149 bug: a re-tag onto a release with no label/catalogue number
    must REMOVE the old ones, not leave them attributed to a release that never
    carried them. FLAC did this; MP3 and M4A silently didn't."""
    path = _copy_fixture(tmp_path, fixture)

    formats.write_tags(path, _tagset(label="Warp", catalog_number="WARP1", media="CD"), None)
    assert formats.read_tags(path).label == "Warp"

    formats.write_tags(path, _tagset(), None)  # same album, release has no label
    after = formats.read_tags(path)
    assert after.label is None
    assert after.catalog_number is None
    assert after.media is None
    assert after.album == "Album"  # the fields that ARE written still land


@pytest.mark.parametrize(("ext", "fixture"), FIXTURES)
def test_write_preserves_tags_harmonist_does_not_own(tmp_path, ext, fixture):
    """The promise not to touch what it doesn't understand. The comment carries
    a recovered Bandcamp URL, the genre is #12's territory, and ReplayGain
    stands in for arbitrary third-party tags."""
    path = _copy_fixture(tmp_path, fixture)
    _set_unowned_tags(path, comment="https://artist.bandcamp.com/album/x", genre="Ambient")

    formats.write_tags(path, _tagset(), None)

    assert formats.read_comment(path) == "https://artist.bandcamp.com/album/x"
    kept = _read_unowned_tags(path)
    assert kept["genre"] == "Ambient"
    assert kept["replaygain"] == "-3.20 dB"


@pytest.mark.parametrize(("ext", "fixture"), FIXTURES)
def test_write_with_no_cover_preserves_embedded_art(tmp_path, ext, fixture):
    """DATA SAFETY: `cover=None` means "leave the art alone" — the path
    `tag_album` uses to protect a compilation's per-track images. Clearing the
    owned set must not touch artwork, which is why it isn't in that set."""
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    path = _copy_fixture(tmp_path, fixture)

    formats.write_tags(path, _tagset(), png)
    embedded = formats.read_cover(path)
    assert embedded is not None

    formats.write_tags(path, _tagset(title="Retagged"), None)
    assert formats.read_cover(path) == embedded


@pytest.mark.parametrize(("ext", "fixture"), FIXTURES)
def test_media_round_trips(tmp_path, ext, fixture):
    """Regression for #149: MP3 wrote `media` to TMED and read it back from
    TXXX:MEDIA, so every MP3 Harmonist tagged reported Media missing on the
    album page — a permanent false difference against MusicBrainz."""
    path = _copy_fixture(tmp_path, fixture)
    formats.write_tags(path, _tagset(media='12" Vinyl'), None)
    assert formats.read_tags(path).media == '12" Vinyl'


def _full_tagset() -> Any:
    """A TagSet with every owned field populated — nothing left at its default,
    so a round trip exercises all of them."""
    from harmonist.formats.types import TagSet

    return TagSet(
        mb_album_id="alb-1",
        album="Music Has the Right to Children",
        album_artist="Boards of Canada",
        title="Roygbiv",
        artist="Boards of Canada",
        track_num=9,
        track_total=18,
        album_artist_sort="Boards of Canada",
        artist_sort="Boards of Canada",
        artists=["Boards of Canada"],
        original_date="1998-04-20",
        script="Latn",
        mb_album_artist_ids=["aa-1"],
        mb_release_group_id="rg-1",
        mb_album_type="Album",
        mb_album_status="official",
        mb_album_country="GB",
        mb_track_id="rec-1",
        mb_release_track_id="rt-1",
        mb_artist_ids=["ar-1"],
        isrcs=["GBAAA9800001"],
        date="1998-04-20",
        disc_num=1,
        disc_total=1,
        label="Warp Records",
        catalog_number="WARP55",
        barcode="5021603055520",
        asin="B000024T4T",
        media="CD",
    )


@pytest.mark.parametrize(("ext", "fixture"), FIXTURES)
def test_every_owned_field_round_trips_through_write_and_back(tmp_path, ext, fixture):
    """`write_tags` returns the owned fields as they were before it wrote, and
    for every one of them that value must come back **identical in type as well
    as content** to what the previous write put there.

    This is the load-bearing test of #86, and it is really a test of silence:
    the tagging audit records a field only when it changed, so any field that
    fails to round-trip — read back as "9" where the TagSet held `9`, or missing
    entirely as `media` did before #149 — reports a phantom change on every
    re-tag forever, and the history fills with edits that never happened.
    """
    from harmonist.formats.owned import Owned

    tagset = _full_tagset()
    path = _copy_fixture(tmp_path, fixture)

    formats.write_tags(path, tagset, None)
    before = formats.write_tags(path, tagset, None)  # returns what the first wrote

    mismatched = {
        f.value: (getattr(tagset, f.value), before.get(f.value))
        for f in Owned
        if getattr(tagset, f.value) != before.get(f.value)
    }
    assert not mismatched


@pytest.mark.parametrize(("ext", "fixture"), FIXTURES)
def test_write_reports_an_untagged_file_as_empty_not_as_matching(tmp_path, ext, fixture):
    """The first tag of a fresh file must report every owned field as absent.

    Absent is `None` for scalars and `[]` for the multi-valued fields, matching
    the TagSet defaults — so "this album had no label" and "this album's label
    was empty" collapse to one state, which is the decision #86 took on empty
    versus missing.
    """
    from harmonist.formats.owned import Owned

    path = _copy_fixture(tmp_path, fixture)
    before = formats.write_tags(path, _full_tagset(), None)

    assert set(before) == {f.value for f in Owned}
    assert all(v in (None, [], 0) for v in before.values()), before


def _set_unowned_tags(path: Path, *, comment: str, genre: str) -> None:
    """Put a comment, a genre and a ReplayGain tag on `path`, natively."""
    if path.suffix == ".mp3":
        from mutagen.id3 import COMM, TCON, TXXX, Encoding
        from mutagen.mp3 import MP3

        audio = MP3(path)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.add(COMM(encoding=Encoding.UTF8, lang="eng", desc="", text=[comment]))
        audio.tags.add(TCON(encoding=Encoding.UTF8, text=[genre]))
        audio.tags.add(
            TXXX(encoding=Encoding.UTF8, desc="REPLAYGAIN_TRACK_GAIN", text=["-3.20 dB"])
        )
        audio.save()
    elif path.suffix in (".m4a", ".mp4"):
        from mutagen.mp4 import MP4

        audio = MP4(path)
        audio["\xa9cmt"] = [comment]
        audio["\xa9gen"] = [genre]
        audio["----:com.apple.iTunes:replaygain_track_gain"] = [b"-3.20 dB"]
        audio.save()
    else:
        audio = _vorbis_open(path)
        audio["COMMENT"] = [comment]
        audio["GENRE"] = [genre]
        audio["REPLAYGAIN_TRACK_GAIN"] = ["-3.20 dB"]
        audio.save()


def _read_unowned_tags(path: Path) -> dict[str, str | None]:
    if path.suffix == ".mp3":
        from mutagen.mp3 import MP3

        tags = MP3(path).tags
        gain = tags.get("TXXX:REPLAYGAIN_TRACK_GAIN")
        genre = tags.get("TCON")
        return {
            "genre": genre.text[0] if genre else None,
            "replaygain": gain.text[0] if gain else None,
        }
    if path.suffix in (".m4a", ".mp4"):
        from mutagen.mp4 import MP4

        audio = MP4(path)
        gain = audio.get("----:com.apple.iTunes:replaygain_track_gain")
        genre = audio.get("\xa9gen")
        return {
            "genre": genre[0] if genre else None,
            "replaygain": bytes(gain[0]).decode() if gain else None,
        }
    audio = _vorbis_open(path)
    return {
        "genre": (audio.get("GENRE") or [None])[0],
        "replaygain": (audio.get("REPLAYGAIN_TRACK_GAIN") or [None])[0],
    }


@pytest.mark.parametrize(("ext", "fixture"), FIXTURES)
def test_tagging_result_does_not_depend_on_what_was_tagged_before(tmp_path, ext, fixture):
    """Tagging is idempotent, and — the stronger property clearing buys — its
    result is independent of the file's tagging history.

    Re-tagging onto a different release used to leave the previous one's
    residue on MP3 and M4A, so a file tagged A-then-B differed from one tagged
    B outright. That made the operation non-idempotent in the way that matters:
    the outcome depended on what had happened to the file before.
    """
    release_a = _tagset(album="First", label="Warp", catalog_number="WARP1", media="CD")
    release_b = _tagset(album="Second", barcode="5099999999999")

    history = _copy_fixture(tmp_path, fixture)
    formats.write_tags(history, release_a, None)
    formats.write_tags(history, release_b, None)

    fresh_dir = tmp_path / "fresh"
    fresh_dir.mkdir()
    fresh = fresh_dir / fixture
    shutil.copy(FIXTURES_DIR / fixture, fresh)
    formats.write_tags(fresh, release_b, None)

    assert formats.read_tags(history) == formats.read_tags(fresh)

    # And tagging the same release twice changes nothing the second time.
    before = formats.read_tags(history)
    formats.write_tags(history, release_b, None)
    assert formats.read_tags(history) == before


# ---------- the partial write an undo needs (#157) ----------


@pytest.mark.parametrize(("ext", "fixture"), FIXTURES)
def test_write_owned_lands_the_same_values_as_write_tags(tmp_path, ext, fixture):
    """The anti-drift test for the two write paths.

    `write_owned` re-implements the field -> frame/atom/key mapping that
    `write_tags` performs, and if the two ever disagree an undo would write a
    value somewhere the reader doesn't look — the exact failure #149 fixed for
    `media`, where the writer used TMED and the reader TXXX:MEDIA so the field
    never round-tripped.

    Tag one copy through the tagger, snapshot it, and replay that snapshot onto
    a fresh copy through `write_owned`. Reading both back must give the same
    answer for every owned field.
    """
    tagged_dir = _make_album(tmp_path / "tagged", fixture)
    tag_album(tagged_dir, _release_one_track())
    tagged = next(tagged_dir.glob(f"*{ext}"))
    snapshot = formats.read_owned(tagged)

    replayed_dir = _make_album(tmp_path / "replayed", fixture)
    replayed = next(replayed_dir.glob(f"*{ext}"))
    formats.write_owned(replayed, snapshot)

    assert formats.read_owned(replayed) == snapshot
    # And through the user-facing reader too, which looks at a different subset.
    assert formats.read_tags(replayed) == formats.read_tags(tagged)


@pytest.mark.parametrize(("ext", "fixture"), FIXTURES)
def test_write_owned_removes_a_field_it_is_given_as_absent(tmp_path, ext, fixture):
    """Absence is a value: reverting a tag that wasn't there before has to
    REMOVE it, not blank it. `write_tags` can't express this — `TagSet.title`
    and friends are required and written unconditionally — which is why the
    undo needs its own primitive."""
    d = _make_album(tmp_path, fixture)
    tag_album(d, _release_one_track())
    f = next(d.glob(f"*{ext}"))

    snapshot = formats.read_owned(f)
    assert snapshot["label"] == "Test Label"
    assert snapshot["isrcs"] == ["GBFMT2100001"]

    formats.write_owned(f, {**snapshot, "label": None, "isrcs": []})

    after = formats.read_owned(f)
    assert after["label"] is None
    assert after["isrcs"] == []
    # Absent, not present-and-empty: the comparison would render "" as a value.
    assert formats.read_tags(f).label is None
    # Everything else is untouched.
    assert after["album"] == "Format Album"
    assert after["mb_album_id"] == "rel-fmt-1"


@pytest.mark.parametrize(("ext", "fixture"), FIXTURES)
def test_write_owned_is_idempotent(tmp_path, ext, fixture):
    """Replaying a snapshot onto a file that already matches changes nothing —
    an undo run twice must be a no-op, like every other transition here."""
    d = _make_album(tmp_path, fixture)
    tag_album(d, _release_one_track())
    f = next(d.glob(f"*{ext}"))

    snapshot = formats.read_owned(f)
    formats.write_owned(f, snapshot)
    assert formats.read_owned(f) == snapshot
    formats.write_owned(f, snapshot)
    assert formats.read_owned(f) == snapshot


@pytest.mark.parametrize(("ext", "fixture"), FIXTURES)
def test_write_owned_returns_the_state_it_replaced(tmp_path, ext, fixture):
    """Like `write_tags`, so an undo can record what IT changed without a
    second read — and so the undo is itself undoable."""
    d = _make_album(tmp_path, fixture)
    tag_album(d, _release_one_track())
    f = next(d.glob(f"*{ext}"))

    snapshot = formats.read_owned(f)
    before = formats.write_owned(f, {**snapshot, "album": "Something Else"})

    assert before == snapshot
    assert formats.read_owned(f)["album"] == "Something Else"


@pytest.mark.parametrize(("ext", "fixture"), FIXTURES)
def test_write_owned_leaves_the_comment_and_the_artwork_alone(tmp_path, ext, fixture):
    """The undo must not destroy what it isn't undoing: a recovered Bandcamp
    URL in the comment, and the embedded artwork, which has its own undo (#131)
    and its own store. Same guarantee `write_tags` gives."""
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    d = _make_album(tmp_path, fixture)
    f = next(d.glob(f"*{ext}"))
    _set_unowned_tags(f, comment="https://artist.bandcamp.com/album/x", genre="Ambient")
    tag_album(d, _release_one_track())
    formats.write_cover(f, png)
    embedded = formats.read_cover(f)
    assert embedded is not None

    snapshot = formats.read_owned(f)
    formats.write_owned(f, {**snapshot, "album": "Renamed"})

    assert formats.read_owned(f)["album"] == "Renamed"
    assert formats.read_comment(f) == "https://artist.bandcamp.com/album/x"
    assert formats.read_cover(f) == embedded
    kept = _read_unowned_tags(f)
    assert kept["genre"] == "Ambient"
    assert kept["replaygain"] == "-3.20 dB"


@pytest.mark.parametrize(("ext", "fixture"), FIXTURES)
def test_read_owned_raises_on_a_file_it_cannot_open(tmp_path, ext, fixture):
    """Not blanks. A revert that read an unreadable file as an untagged one
    would decide every field had already changed and silently do nothing —
    #112's lesson, one layer down."""
    broken = tmp_path / f"broken{ext}"
    broken.write_bytes(b"not audio at all")

    # MutagenError from the formats that open the file directly, OSError from
    # the Vorbis ones, whose `_open` swallows and returns None. Either is a
    # refusal, which is the property under test.
    with pytest.raises((OSError, MutagenError)):
        formats.read_owned(broken)


def _vorbis_open(path: Path) -> Any:
    if path.suffix == ".flac":
        from mutagen.flac import FLAC

        return FLAC(path)
    from mutagen.oggopus import OggOpus

    return OggOpus(path)


@pytest.mark.parametrize(
    "trck, tpos, want_track_total, want_disc_total",
    [
        ("3/12", "1/2", 12, 2),
        ("3", "1", None, None),  # bare number carries no total
        ("3/x", "1/y", None, None),  # unparseable
        (None, None, None, None),
    ],
)
def test_mp3_scan_fields_read_the_totals(tmp_path, trck, tpos, want_track_total, want_disc_total):
    """#195 reads COMPLETE vs INCOMPLETE off these, so a bare "3" must give None
    rather than 3 — "no total recorded" and "a one-track release" are different
    claims and only one of them is true."""
    from mutagen.id3 import TPOS, TRCK
    from mutagen.mp3 import MP3

    src = FIXTURES_DIR / "sine.mp3"
    if not src.exists():
        pytest.skip("no mp3 fixture")
    target = tmp_path / "t.mp3"
    shutil.copy(src, target)
    audio = MP3(target)
    if audio.tags is None:
        audio.add_tags()
    if trck:
        audio.tags.add(TRCK(encoding=3, text=[trck]))
    if tpos:
        audio.tags.add(TPOS(encoding=3, text=[tpos]))
    audio.save()

    sf = formats.read_scan_fields(target)
    assert sf.track_total == want_track_total
    assert sf.disc_total == want_disc_total


# ---------- a second tagging is a real no-op, per format (#266) ----------


@pytest.mark.parametrize(("ext", "fixture"), FIXTURES)
def test_a_second_tagging_of_an_unchanged_release_writes_nothing(tmp_path, ext, fixture):
    """The whole file left alone — not merely re-written with the same values.

    This is the mechanical guard on `has_superseded_tags`. That check exists so a
    skipped write can't strand a retired tag on disk, and it is dangerously easy
    to make it True for a tag Harmonist itself writes every time — `ORIGINALYEAR`
    is exactly that shape, unread by `read_owned` and present on every correctly
    tagged file. Declaring one of those superseded would make every album on the
    library re-write on every pass forever, which no assertion about tag VALUES
    can see: the values would be right each time. The mtime is what notices.

    Parametrized because the backends declare that list separately, so the trap
    is per-format and only a per-format test covers all of them.
    """
    d = _make_album(tmp_path, fixture)
    f = next(d.glob(f"*{ext}"))
    tag_album(d, _release_one_track())
    os.utime(f, (1_000_000, 1_000_000))

    tag_album(d, _release_one_track())

    assert f.stat().st_mtime == 1_000_000


@pytest.mark.parametrize(("ext", "fixture"), FIXTURES)
def test_a_freshly_tagged_file_carries_no_superseded_tag(tmp_path, ext, fixture):
    """The same trap, stated where it is caused rather than where it shows up: a
    tag Harmonist has just written is by definition not one a write would remove.
    """
    d = _make_album(tmp_path, fixture)
    tag_album(d, _release_one_track())

    assert not formats.has_superseded_tags(next(d.glob(f"*{ext}")))


def test_a_superseded_atom_is_reported_so_a_write_can_clear_it(tmp_path):
    """MP4's retired `MUSICBRAINZ_RELEASEID`, the one spelling this really covers.

    Not parametrized: it is the only superseded tag any backend declares, and a
    format-agnostic version would assert nothing on the other three.
    """
    d = _make_album(tmp_path, "sine.m4a")
    tag_album(d, _release_one_track())
    f = next(d.glob("*.m4a"))
    audio = MP4(f)
    audio[m4a.LEGACY_RELEASE_ID] = [b"old-broken-mbid"]
    audio.save()

    assert formats.has_superseded_tags(f)


def test_an_unsupported_extension_has_no_superseded_tags(tmp_path):
    """Nothing here writes it, so nothing here has anything to clear."""
    assert not formats.has_superseded_tags(tmp_path / "cover.jpg")
