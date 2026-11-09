"""A sandboxed CD rip whose only evidence is a table of contents in its tags."""

import os
import shutil
from pathlib import Path

from mutagen.mp4 import MP4

from harmonist import demo, mb_lookup, mb_search, sidecar
from harmonist.models import Sidecar
from harmonist.web.main import create_app

FIRST = "6b072dd5-c6fd-4140-a915-aaedf2c85115"
_seed = demo.seed


def seed(music_dir, **kwargs):
    _seed(music_dir, **kwargs)
    folder = music_dir / "LCD Soundsystem" / "American Dream"
    folder.mkdir(parents=True)
    file = folder / "01 Oh Baby.m4a"
    shutil.copy(Path(__file__).parents[1] / "fixtures" / "sine.m4a", file)
    audio = MP4(file)
    audio.clear()
    audio["©alb"] = ["American Dream"]
    audio["©ART"] = ["LCD Soundsystem"]
    audio["aART"] = ["LCD Soundsystem"]
    audio["©nam"] = ["Oh Baby"]
    audio["trkn"] = [(1, 1)]
    audio["----:com.apple.iTunes:iTunes_CDDB_1"] = [b"02000F01+1000+1+150"]
    audio.save()
    sidecar.write(folder, Sidecar(temp_uid="disc-id-demo"))


demo.MB_RELEASES[FIRST] = {
    "id": FIRST,
    "title": "American Dream",
    "date": "2017-09-01",
    "country": "XE",
    "status": "Official",
    "artist-credit-phrase": "LCD Soundsystem",
    "artist-credit": [
        {
            "artist": {
                "id": "demo-lcd",
                "name": "LCD Soundsystem",
                "sort-name": "LCD Soundsystem",
            }
        }
    ],
    "label-info-list": [{"catalog-number": "88985456102", "label": {"name": "Columbia"}}],
    "medium-list": [
        {
            "position": "1",
            "format": "CD",
            "track-count": "1",
            "track-list": [
                {
                    "position": "1",
                    "number": "1",
                    "length": "1000",
                    "recording": {"id": "demo-recording-lcd", "title": "Oh Baby"},
                }
            ],
        }
    ],
}

demo.seed = seed
app = create_app()


def search_disc_id(disc_id):
    scenario = os.environ.get("HARMONIST_TEST_DISC_ID_RESULTS", "1")
    if scenario == "error":
        raise mb_search.MBSearchError("MusicBrainz unavailable")
    if scenario == "0":
        return [], 0
    return [mb_lookup.release_summary(demo.MB_RELEASES[FIRST])], 1


mb_search.search_disc_id = search_disc_id
