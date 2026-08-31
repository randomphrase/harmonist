"""The album page shows video tracks it cannot tag (#226).

*DJ Shadow — Live! In Tune and on Time* is a 21-track CD plus a 29-track DVD, of
which 26 videos are on disk — correctly named, correctly numbered, tagged by
Picard like everything else. The page said:

    All 21 tracks match MusicBrainz · Disc 2 not on disk
      Not on disk: DISC 2 · DVD, 29 tracks
         ▸ None of this disc's 29 tracks are on disk

Twenty-six of them are right there. The scanner had already learnt to count
video towards completeness (#193) and derived INCOMPLETE at 47 of 50, which is
correct; the comparison behind the page was still reading `audio_files` alone,
so every DVD track compared as MISSING and the disc read as absent (#216).

The other half of the fix is what the page must NOT claim. Harmonist cannot tag
video (#66), so a video track is reported as present and nothing more: comparing
it field by field would raise differences that no re-tag could ever settle, and
its length is the video's runtime — intros, credits and all — which MusicBrainz
often does not have at all. Measured against the real release, ten of the
twenty-six lengths disagree, five of them because MusicBrainz records none.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mutagen.mp4 import MP4

from harmonist import album_files, compare, formats, mb_lookup
from harmonist import sidecar as sidecar_mod
from harmonist.compare import MBTrack, Medium, TrackState
from harmonist.config import BandcampConfig, Config, PathsConfig, ServerConfig, TestConfig
from harmonist.formats.types import TagSet, TrackTags
from harmonist.models import Sidecar
from harmonist.web.main import create_app

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SINE_M4A = FIXTURES_DIR / "sine.m4a"
MBID = "22222222-6666-4444-8888-999999999999"


# ---------------------------------------------------------------------------
# Reading a video file
# ---------------------------------------------------------------------------


def _video(path: Path, *, disc: int = 2, track: int = 1, total: int = 4, title: str) -> Path:
    """A Picard-tagged `.m4v`, as the DVD rips in the library actually are."""
    shutil.copy(SINE_M4A, path)
    a = MP4(path)
    a["\xa9nam"] = [title]
    a["\xa9alb"] = ["Live! In Tune and on Time"]
    a["\xa9ART"] = ["DJ Shadow"]
    a["aART"] = ["DJ Shadow"]
    a["trkn"] = [(track, total)]
    a["disk"] = [(disc, 2)]
    a["----:com.apple.iTunes:LABEL"] = [b"Geffen Records"]
    a["----:com.apple.iTunes:MEDIA"] = [b"DVD"]
    a["----:com.apple.iTunes:MusicBrainz Album Id"] = [MBID.encode()]
    a.save()
    return path


def test_a_video_file_carries_the_whole_picard_tag_set(tmp_path):
    """The premise of the whole issue: an `.m4v` is an MP4 container, so a
    Picard-tagged video states its album, its disc and its position exactly as
    the audio beside it does. There was never anything to guess."""
    tags = formats.read_video_tags(_video(tmp_path / "2-05 Intro.m4v", track=5, title="Intro"))

    assert tags.title == "Intro"
    assert tags.album == "Live! In Tune and on Time"
    assert tags.artist == "DJ Shadow"
    assert tags.label == "Geffen Records"
    assert (tags.disc_num, tags.track_num) == (2, 5)
    assert tags.duration_ms is not None
    assert tags.video is True, "and it says which half of the album it came from"


def test_the_tag_writing_dispatch_still_refuses_video(tmp_path):
    """The guard that keeps this a READ (#66). `.m4v` stays out of the module
    table that `read_tags` and every write goes through, so the fix cannot leak
    into the tagger by way of a shared reader."""
    f = _video(tmp_path / "2-01 Intro.m4v", title="Intro")

    assert formats.is_supported(f) is False
    assert formats.read_tags(f) == TrackTags(), "the writing dispatch has no opinion on video"
    assert f not in album_files.for_paths([tmp_path]), "and the tagger's file list never sees it"
    assert album_files.videos_for_paths([tmp_path]) == [f]


# ---------------------------------------------------------------------------
# What the comparison makes of one
# ---------------------------------------------------------------------------


def _mb(disc: int, n: int, title: str, total: int, length: int | None = 200_000) -> MBTrack:
    return MBTrack(
        tags=TagSet(
            title=title,
            album="Live! In Tune and on Time",
            artist="DJ Shadow",
            mb_album_id=MBID,
            album_artist="DJ Shadow",
            track_total=total,
            disc_num=disc,
            track_num=n,
        ),
        length_ms=length,
    )


def _owned(title: str, disc: int, n: int, total: int) -> dict[str, object]:
    """The snapshot a real `read_tags` takes, matching `_mb`'s track exactly.

    Named rather than inlined into both helpers below because the two sides have
    to agree: since #309 a per-track tag MusicBrainz has and the file does not is
    a difference that earns a COLUMN, so a fixture short of one changes the shape
    of the table under every assertion here.
    """
    return {
        "title": title,
        "artist": "DJ Shadow",
        "album": "Live! In Tune and on Time",
        "album_artist": "DJ Shadow",
        "disc_num": disc,
        "track_num": n,
        "track_total": total,
    }


def _audio_track(n: int, title: str, total: int = 2) -> tuple[str, TrackTags]:
    return (
        f"1-{n:02d} {title}.m4a",
        TrackTags(
            title=title,
            artist="DJ Shadow",
            disc_num=1,
            track_num=n,
            duration_ms=200_000,
            owned=_owned(title, 1, n, total),
        ),
    )


def _video_track(
    n: int, title: str, *, duration: int = 200_000, total: int = 4
) -> tuple[str, TrackTags]:
    return (
        f"2-{n:02d} {title}.m4v",
        TrackTags(
            title=title,
            artist="DJ Shadow",
            disc_num=2,
            track_num=n,
            duration_ms=duration,
            video=True,
            owned=_owned(title, 2, n, total),
        ),
    )


def _dvd_release(present: int, of: int = 4):
    """A 2-track CD and an `of`-track DVD with `present` of its videos on disk."""
    mb = [_mb(1, i, f"Song {i}", 2) for i in range(1, 3)]
    mb += [_mb(2, i, f"Video {i}", of) for i in range(1, of + 1)]
    files = [_audio_track(i, f"Song {i}") for i in range(1, 3)]
    files += [_video_track(i, f"Video {i}") for i in range(1, present + 1)]
    return files, mb


def _media():
    return [Medium(1, None, "CD"), Medium(2, None, "DVD")]


def test_a_video_on_disk_is_a_present_track():
    """The bug itself: nothing had ever handed a video file to the comparison,
    so every one of them compared as MISSING."""
    files, mb = _dvd_release(present=4)

    rows = compare.tracklist(files, mb, _media()).tracks

    assert [t.state for t in rows] == [TrackState.PRESENT] * 6
    assert [t.video for t in rows] == [False, False, True, True, True, True]
    assert [t.file_name for t in rows[2:]] == [f"2-0{i} Video {i}.m4v" for i in range(1, 5)]


def test_a_partly_ripped_dvd_is_short_not_absent():
    """The symptom on the page. `DiscGroup.absent` collapses a disc nobody
    ripped into one line (#216) — which is right for a DVD the user declined and
    flatly wrong for one they have 26 of."""
    files, mb = _dvd_release(present=3)

    t = compare.tracklist(files, mb, _media())
    dvd = t.discs[1]

    assert dvd.absent is False
    assert [r.state for r in dvd.tracks] == [TrackState.PRESENT] * 3 + [TrackState.MISSING]
    assert t.summary == "1 of 6 tracks differs from MusicBrainz · 1 not on disk"
    assert "Disc 2 not on disk" not in t.summary


def test_a_video_track_is_reported_as_present_and_nothing_more():
    """Harmonist will never re-tag a video, so a difference found in one is a
    finding the user cannot act on — it would sit on the page for good."""
    files, mb = _dvd_release(present=1)
    files[-1] = _video_track(1, "Intro (live)")  # MusicBrainz calls it "Video 1"

    row = compare.tracklist(files, mb, _media()).discs[1].tracks[0]

    assert row.state is TrackState.PRESENT
    assert row.differs is False, "no finding against a file nothing can change"
    assert row.shows_mb is False, "and so no MusicBrainz line beneath it"
    # Every column the table has, carrying this file's own values and nothing
    # else. There is no Artist column: every track on this release is credited
    # to DJ Shadow and so is the album, so it collapsed (#309) — which is a fact
    # about the release, not about the video.
    assert [f.disk for f in row.fields] == ["2-1", "Intro (live)", "3:20"]
    assert all(f.agreement is compare.Agreement.ONLY_DISK for f in row.fields)


@pytest.mark.parametrize("mb_length", [75_300, None])
def test_a_videos_length_is_never_compared(mb_length):
    """A DVD track's runtime is the VIDEO's — *Intro* is 2:50 on disk against
    MusicBrainz's 1:15 for the recording — and for five of the real disc's
    tracks MusicBrainz has no length at all. Neither is a defect in the file."""
    files, mb = _dvd_release(present=1)
    mb[2] = _mb(2, 1, "Video 1", 4, length=mb_length)

    row = compare.tracklist(files, mb, _media()).discs[1].tracks[0]
    length = row.fields[-1]

    assert length.differs is False
    assert length.disk == "3:20", "the file's own runtime is still shown"
    assert length.mb is None


def test_an_unreadable_video_still_says_so(tmp_path):
    """`video` rides alongside `unreadable`, it doesn't replace it: a video file
    that won't open is a failing disk, and must not read as merely untaggable."""
    row = compare.disk_tracklist([("2-01 Intro.m4v", TrackTags(unreadable=True, video=True))])

    assert row.tracks[0].state is TrackState.UNREADABLE


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg(tmp_path):
    return Config(
        paths=PathsConfig(config_dir=tmp_path / "config", music_dir=tmp_path / "music"),
        bandcamp=BandcampConfig(),
        server=ServerConfig(),
        test=TestConfig(mode="fixture"),
    )


@pytest.fixture
def client(cfg):
    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.config_dir.mkdir(parents=True, exist_ok=True)
    # HX-Request: the CSRF middleware requires it on every state-changing call.
    return TestClient(create_app(cfg), headers={"HX-Request": "true"})


def _album_on_disk(cfg, *, videos: int, of: int = 4) -> Path:
    """A CD of 2 audio tracks plus `videos` of the DVD's `of` videos."""
    d = cfg.paths.music_dir / "DJ Shadow" / "In Tune and On Time"
    d.mkdir(parents=True)
    for i in range(1, 3):
        f = d / f"1-{i:02d} Song {i}.m4a"
        shutil.copy(SINE_M4A, f)
        a = MP4(f)
        a["\xa9nam"] = [f"Song {i}"]
        a["\xa9alb"] = ["Live! In Tune and on Time"]
        a["\xa9ART"] = ["DJ Shadow"]
        a["aART"] = ["DJ Shadow"]
        a["trkn"] = [(i, 2)]
        a["disk"] = [(1, 2)]
        a["----:com.apple.iTunes:MusicBrainz Album Id"] = [MBID.encode()]
        # Tagged by Picard like everything else, which is what the module
        # docstring says these files are — the per-track MusicBrainz ids and the
        # multi-value artist included. Left off, they read as real differences
        # against the release and the CD half of this album stops being the
        # quiet control the video half is measured against (#309).
        a["----:com.apple.iTunes:MusicBrainz Release Track Id"] = [f"rt-1-{i}".encode()]
        a["----:com.apple.iTunes:MusicBrainz Track Id"] = [f"rec-1-{i}".encode()]
        a["----:com.apple.iTunes:MusicBrainz Artist Id"] = [b"art-1"]
        a["----:com.apple.iTunes:ARTISTS"] = [b"DJ Shadow"]
        a["----:com.apple.iTunes:MEDIA"] = [b"CD"]
        a.save()
    for i in range(1, videos + 1):
        _video(d / f"2-{i:02d} Video {i}.m4v", track=i, total=of, title=f"Video {i}")
    sidecar_mod.write(
        d, Sidecar(mb_release_id=MBID, tagged_at=datetime(2026, 1, 1, tzinfo=UTC), video_media=(2,))
    )
    return d


def _release(of: int = 4) -> dict:
    """The release the files name. The CD's lengths match the fixture's 1s; the
    DVD's deliberately do not — a video's runtime is its own, and if the page
    ever starts comparing it, these tests go red rather than the user's page
    filling with differences."""
    return {
        "id": MBID,
        "title": "Live! In Tune and on Time",
        "release-group": {"id": "rg-1", "primary-type": "Album"},
        "artist-credit": [{"artist": {"id": "art-1", "name": "DJ Shadow"}, "name": "DJ Shadow"}],
        "medium-list": [
            {
                "position": "1",
                "format": "CD",
                "track-list": [
                    {
                        "id": f"rt-1-{i}",
                        "position": str(i),
                        "title": f"Song {i}",
                        "recording": {"id": f"rec-1-{i}", "title": f"Song {i}", "length": "1000"},
                    }
                    for i in range(1, 3)
                ],
            },
            {
                "position": "2",
                "format": "DVD",
                "track-list": [
                    {
                        "id": f"rt-2-{i}",
                        "position": str(i),
                        "title": f"Video {i}",
                        "recording": {"id": f"rec-2-{i}", "title": f"Video {i}", "length": "75300"},
                    }
                    for i in range(1, of + 1)
                ],
            },
        ],
    }


def _album_id(cfg, album_dir: Path) -> str:
    from harmonist import scanner

    for a in scanner.scan(cfg.paths.music_dir):
        if a.path == album_dir:
            return a.id
    raise AssertionError(f"no album at {album_dir}")


def test_the_page_lists_the_videos_that_are_on_disk(client, cfg, monkeypatch):
    """End to end, the reported symptom: the page is being consulted to explain
    why the album is incomplete, and it was answering with the one thing that is
    provably untrue."""
    d = _album_on_disk(cfg, videos=3)
    monkeypatch.setattr(mb_lookup, "fetch_release", lambda mbid: _release())

    body = client.get(f"/library/{_album_id(cfg, d)}/compare").text

    assert "None of this disc's" not in body, "the line the issue is named for"
    assert "Disc 2 not on disk" not in body
    for i in (1, 2, 3):
        assert f"Video {i}" in body
    assert body.count("Not on disk") == 1, "only the video that really is missing"
    assert "1 of 6 tracks differs from MusicBrainz · 1 not on disk" in body


def test_the_page_marks_a_video_as_one(client, cfg, monkeypatch):
    """A row that never compares and never changes when the album is re-tagged
    has to say why, or it reads as a bug in the page."""
    d = _album_on_disk(cfg, videos=4)
    monkeypatch.setattr(mb_lookup, "fetch_release", lambda mbid: _release())

    body = client.get(f"/library/{_album_id(cfg, d)}/compare").text

    assert body.count("track-diff__video") == 4, "one mark per video, none on the CD's tracks"
    assert "Harmonist reads its tags but never writes them" in body


def test_the_album_panel_still_speaks_for_the_audio_alone(client, cfg, monkeypatch):
    """The album-level fields are a consensus of the tracks, and a re-tag only
    ever moves the audio. Counting video in would make the first re-tag of any
    album with a bonus DVD report a disagreement it can never resolve."""
    d = _album_on_disk(cfg, videos=4)
    for f in sorted(d.glob("*.m4v")):
        a = MP4(f)
        a["----:com.apple.iTunes:LABEL"] = [b"Mo Wax"]  # stale: the CD says Geffen
        a.save()
    monkeypatch.setattr(mb_lookup, "fetch_release", lambda mbid: _release())

    body = client.get(f"/library/{_album_id(cfg, d)}/compare").text

    assert "Mo Wax" not in body, "the panel is the audio's account of the album"
    assert "4 tracks differ" not in body
