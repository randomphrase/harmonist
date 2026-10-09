"""Audio format is file evidence, never a proposed MusicBrainz tag change."""

from dataclasses import replace
from pathlib import Path

import pytest

from harmonist import compare, formats
from harmonist.formats.types import TagSet


def _tracks(*extensions):
    return [
        (
            f"{i}.{ext}",
            replace(
                formats.read_tags(Path(__file__).parent / "fixtures" / f"sine.{ext}"),
                title=f"Track {i}",
                track_num=i,
                disc_num=1,
                owned={"title": f"Track {i}", "track_num": i, "disc_num": 1},
            ),
        )
        for i, ext in enumerate(extensions, 1)
    ]


@pytest.mark.parametrize("with_mb", [False, True])
def test_mixed_formats_are_located_by_track_without_becoming_tag_changes(with_mb):
    tracks = _tracks("flac", "m4a", "flac")
    if with_mb:
        mb = [
            compare.MBTrack(
                TagSet(
                    mb_album_id="release",
                    album="Album",
                    album_artist="Artist",
                    title=f"Track {i}",
                    artist="Artist",
                    track_num=i,
                    track_total=4,
                )
            )
            for i in range(1, 5)
        ]
        view = compare.tracklist(tracks, mb)
    else:
        view = compare.disk_tracklist(tracks)

    assert "Format" in [c.label for c in view.columns]
    index = next(i for i, c in enumerate(view.columns) if c.label == "Format")
    cells = [t.fields[index] for t in view.tracks]
    assert [f.disk for f in cells[:3]] == [
        "FLAC · 44.1 kHz · 16 bit",
        "ALAC · 44.1 kHz · 16 bit",
        "FLAC · 44.1 kHz · 16 bit",
    ]
    assert all(not f.differs and f.mb is None for f in cells)
    assert all(len(t.fields) == len(view.columns) for t in view.tracks)
    if with_mb:
        assert cells[-1].disk is None, "a missing file has no format to report"


def test_uniform_format_is_stated_once_beneath_the_tracks():
    view = compare.disk_tracklist(_tracks("flac", "flac"))

    assert [(f.label, f.value) for f in view.collapsed if f.label == "Format"] == [
        ("Format", "FLAC · 44.1 kHz · 16 bit")
    ]
    assert "Format" not in [c.label for c in view.columns]
    assert "Format" not in view.shown_fields


def test_unreadable_audio_prevents_a_false_uniform_format_claim():
    tracks = _tracks("flac", "flac")
    tracks.append(("broken.flac", formats.TrackTags(unreadable=True)))
    view = compare.disk_tracklist(tracks)

    assert "Format" in [c.label for c in view.columns]
    assert not any(f.label == "Format" for f in view.collapsed)
    assert view.tracks[-1].state is compare.TrackState.UNREADABLE
    assert next(f for f in view.tracks[-1].fields if f.label == "Format").disk is None


def test_video_does_not_make_a_uniform_audio_format_mixed():
    tracks = _tracks("flac", "m4a")
    name, tags = tracks[-1]
    tracks[-1] = name, replace(tags, video=True)
    view = compare.disk_tracklist(tracks)

    assert [(f.label, f.value) for f in view.collapsed if f.label == "Format"] == [
        ("Format", "FLAC · 44.1 kHz · 16 bit")
    ]


def test_vbr_rates_collapse_but_a_different_sample_rate_earns_a_column():
    from harmonist.formats.quality import AudioQuality

    tracks = [
        (
            str(i),
            formats.TrackTags(
                codec="MP3",
                quality=AudioQuality(
                    sample_rate=44100, bitrate=rate, bitrate_mode="VBR", bitrate_varies=True
                ),
            ),
        )
        for i, rate in enumerate((128000, 192000, 256000))
    ]
    uniform = compare.disk_tracklist(tracks)
    assert [(f.label, f.value) for f in uniform.collapsed if f.label == "Format"] == [
        ("Format", "MP3 · 44.1 kHz · 192 kbps VBR")
    ]
    name, tags = tracks[-1]
    tracks[-1] = name, replace(tags, quality=tags.quality._replace(sample_rate=48000))
    mixed = compare.disk_tracklist(tracks)
    assert "Format" in [c.label for c in mixed.columns]
    assert next(f for f in mixed.tracks[-1].fields if f.label == "Format").disk == (
        "MP3 · 48 kHz · 256 kbps VBR"
    )
