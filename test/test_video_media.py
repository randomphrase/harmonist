"""An absent medium that was video shouldn't count against completeness (#206).

The rule, and the word doing the work is *complete*:

    if the missing tracks form one or more COMPLETE missing media, and those
    tracks are all video, don't mark the album incomplete — show the missing
    discs on its page instead.

A partly-present video medium stays incomplete, on the principle that if the
user has one video they should have the rest. That is what separates "I chose
not to rip the DVD" from "my DVD rip failed halfway".

Real cases from the dogfooded library, with MusicBrainz's actual answers:

    Midnight Oil — Best of Both Worlds  DVD-Video 44 + CD 16     video media (1,)
    TISM — The White Albun              DVD 22 + CD 16 + DVD 31  video media (1, 3)
    DJ Shadow — In Tune and on Time     CD 21 + DVD 29           video media (2,)
    Pink Floyd — Wish You Were Here 50  Blu-ray 49 (45 audio)    video media ()
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from mutagen.mp4 import MP4

from harmonist import reconcile
from harmonist import sidecar as sidecar_mod
from harmonist.models import AlbumState, Sidecar
from harmonist.scanner import scan

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SINE_M4A = FIXTURES_DIR / "sine.m4a"
MBID = "11111111-2222-3333-4444-555555555555"


def _album(
    root: Path,
    *,
    tracks: int,
    disc: int,
    disc_total: int,
    video_media: tuple[int, ...] | None = None,
    videos: int = 0,
    video_disc: int | None = None,
    video_total: int | None = None,
) -> Path:
    """An album holding ONE medium of a multi-disc release, optionally with some
    of another medium present as video files."""
    d = root / "Artist" / "Album"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(1, tracks + 1):
        f = d / f"{disc}-{i:02d} Track {i}.m4a"
        shutil.copy(SINE_M4A, f)
        a = MP4(f)
        a["\xa9alb"] = ["Album"]
        a["\xa9ART"] = ["Artist"]
        a["trkn"] = [(i, tracks)]
        a["disk"] = [(disc, disc_total)]
        a["----:com.apple.iTunes:MusicBrainz Album Id"] = [MBID.encode()]
        a.save()
    for i in range(1, videos + 1):
        f = d / f"{video_disc}-{i:02d} Video {i}.m4v"
        shutil.copy(SINE_M4A, f)
        a = MP4(f)
        a["trkn"] = [(i, video_total or videos)]
        a["disk"] = [(video_disc, disc_total)]
        a["----:com.apple.iTunes:MusicBrainz Album Id"] = [MBID.encode()]
        a.save()
    sidecar_mod.write(
        d,
        Sidecar(
            mb_release_id=MBID,
            tagged_at=datetime(2026, 1, 1, tzinfo=UTC),
            video_media=video_media,
        ),
    )
    return d


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def test_an_absent_video_disc_does_not_make_the_album_incomplete(tmp_path):
    """Midnight Oil: the CD is complete, the DVD was never ripped and never
    will be. Being told forever that 44 video tracks are missing is noise."""
    _album(tmp_path, tracks=16, disc=2, disc_total=2, video_media=(1,))

    album = scan(tmp_path)[0]

    assert album.state == AlbumState.COMPLETE
    assert album.absent_media == frozenset({1})


def test_several_absent_video_discs_are_all_forgiven(tmp_path):
    """TISM: a CD sandwiched between two DVDs."""
    _album(tmp_path, tracks=16, disc=2, disc_total=3, video_media=(1, 3))

    album = scan(tmp_path)[0]

    assert album.state == AlbumState.COMPLETE
    assert album.absent_media == frozenset({1, 3})


def test_an_absent_audio_disc_still_makes_the_album_incomplete(tmp_path):
    """The control that matters most. A half-copied 2-CD set is a real defect."""
    _album(tmp_path, tracks=12, disc=1, disc_total=2, video_media=())

    album = scan(tmp_path)[0]

    assert album.state == AlbumState.INCOMPLETE
    assert album.expected_track_count is None, "nothing on disk sizes the absent disc"


def test_a_partly_present_video_disc_is_still_incomplete(tmp_path):
    """DJ Shadow: 26 of the DVD's 29 videos. If you have one video you should
    have the rest — the medium is not ABSENT, it is short."""
    # The DVD has 29 tracks; 26 of them are on disk.
    _album(
        tmp_path,
        tracks=21,
        disc=1,
        disc_total=2,
        video_media=(2,),
        videos=26,
        video_disc=2,
        video_total=29,
    )

    album = scan(tmp_path)[0]

    assert album.absent_media == frozenset(), "disc 2 is present, just incomplete"
    assert album.state == AlbumState.INCOMPLETE


def test_a_fully_present_video_disc_is_complete(tmp_path):
    """Barking, unchanged by #206: both media on disk."""
    _album(tmp_path, tracks=9, disc=1, disc_total=2, video_media=(2,), videos=9, video_disc=2)

    assert scan(tmp_path)[0].state == AlbumState.COMPLETE


def test_before_musicbrainz_is_asked_the_album_stays_incomplete(tmp_path):
    """`None` means "not asked". Assuming an absent disc is video would forgive
    a genuinely missing CD on no evidence at all."""
    _album(tmp_path, tracks=16, disc=2, disc_total=2, video_media=None)

    album = scan(tmp_path)[0]

    assert album.state == AlbumState.INCOMPLETE
    assert reconcile.needs_video_media(album), "and it is queued to be asked"


# ---------------------------------------------------------------------------
# The bounded lookup
# ---------------------------------------------------------------------------


def test_only_albums_missing_a_whole_disc_are_asked(tmp_path):
    """What makes this affordable where #187's per-album backfill was not."""
    _album(tmp_path, tracks=16, disc=2, disc_total=2)
    whole = tmp_path / "Artist" / "Whole"
    whole.mkdir(parents=True)
    shutil.copy(SINE_M4A, whole / "01 Track.m4a")
    a = MP4(whole / "01 Track.m4a")
    a["trkn"] = [(1, 1)]
    a["disk"] = [(1, 1)]
    a["----:com.apple.iTunes:MusicBrainz Album Id"] = [b"other-release"]
    a.save()
    sidecar_mod.write(whole, Sidecar(mb_release_id="other-release", tagged_at=datetime.now(UTC)))

    pending = [a for a in scan(tmp_path) if reconcile.needs_video_media(a)]

    assert len(pending) == 1
    assert pending[0].path.name == "Album"


def test_recording_the_answer_stops_it_being_asked_again(tmp_path):
    d = _album(tmp_path, tracks=16, disc=2, disc_total=2)
    calls: list[str] = []

    def fetch(mbid: str) -> tuple[int, ...]:
        calls.append(mbid)
        return (1,)

    first = reconcile.record_video_media(d, fetch_video_media=fetch)
    second = reconcile.record_video_media(d, fetch_video_media=fetch)

    assert (first, second) == ((1,), None)
    assert len(calls) == 1
    assert scan(tmp_path)[0].state == AlbumState.COMPLETE


def test_a_negative_answer_is_recorded_too(tmp_path):
    """`()` means "asked, none are video" — distinct from "not asked". Without
    that distinction a release with no video would be re-fetched forever."""
    d = _album(tmp_path, tracks=12, disc=1, disc_total=2)

    reconcile.record_video_media(d, fetch_video_media=lambda m: ())

    assert sidecar_mod.read(d).video_media == ()
    album = scan(tmp_path)[0]
    assert album.state == AlbumState.INCOMPLETE
    assert not reconcile.needs_video_media(album), "asked and answered"


def test_a_failed_lookup_records_nothing(tmp_path):
    """Writing "none are video" from a request that never succeeded would mark
    the album incomplete forever on evidence nobody gathered."""
    from harmonist.mb_lookup import MBError

    d = _album(tmp_path, tracks=16, disc=2, disc_total=2)

    def boom(mbid: str) -> tuple[int, ...]:
        raise MBError("MB is down")

    with pytest.raises(MBError):
        reconcile.record_video_media(d, fetch_video_media=boom)
    assert sidecar_mod.read(d).video_media is None


def test_the_lookup_re_reads_rather_than_trusting_a_stale_snapshot(tmp_path):
    d = _album(tmp_path, tracks=16, disc=2, disc_total=2)
    sidecar_mod.write(d, Sidecar(mb_release_id="moved-on", video_media=(9,)))

    assert reconcile.record_video_media(d, fetch_video_media=lambda m: (1,)) is None
    assert sidecar_mod.read(d).video_media == (9,)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_video_media_round_trips_and_distinguishes_empty_from_absent(tmp_path):
    import json

    for name, value in (("asked", ()), ("found", (1, 3)), ("unasked", None)):
        d = tmp_path / name
        d.mkdir()
        sidecar_mod.write(d, Sidecar(mb_release_id=MBID, video_media=value))
        assert sidecar_mod.read(d).video_media == value

    written = json.loads(sidecar_mod.sidecar_path(tmp_path / "unasked").read_text())
    assert "video_media" not in written, "the default is omitted"
    asked = json.loads(sidecar_mod.sidecar_path(tmp_path / "asked").read_text())
    assert asked["video_media"] == [], "but an empty answer is not the default"
