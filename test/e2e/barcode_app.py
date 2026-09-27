"""A sandboxed Qobuz-style album with two MusicBrainz barcode candidates."""

import os
import shutil
from pathlib import Path

from mutagen.mp4 import MP4

from harmonist import demo, mb_lookup, mb_search, sidecar
from harmonist.models import Sidecar
from harmonist.web.main import create_app

FIRST = "b1a3a871-16c2-4e8b-b775-22785decf471"
SECOND = "2bf4bf22-ddb6-41da-8870-8913bd7f3f5a"
_seed = demo.seed


def seed(music_dir, **kwargs):
    _seed(music_dir, **kwargs)
    folder = music_dir / "LFO" / "Frequencies"
    folder.mkdir(parents=True)
    file = folder / "01 LFO.m4a"
    shutil.copy(Path(__file__).parents[1] / "fixtures" / "sine.m4a", file)
    audio = MP4(file)
    audio.clear()
    audio["©alb"] = ["Frequencies"]
    audio["©ART"] = ["LFO"]
    audio["aART"] = ["LFO"]
    audio["©nam"] = ["LFO"]
    audio["trkn"] = [(1, 1)]
    audio["----:com.apple.iTunes:UPC"] = [b"0801061000332"]
    audio.save()
    sidecar.write(folder, Sidecar(temp_uid="barcode-demo"))


for mbid, barcode in ((FIRST, "0801061000332"), (SECOND, "801061000332")):
    demo.MB_RELEASES[mbid] = {
        "id": mbid,
        "title": "Frequencies",
        "barcode": barcode,
        "artist-credit-phrase": "LFO",
        "artist-credit": [{"artist": {"id": "demo-lfo", "name": "LFO", "sort-name": "LFO"}}],
        "medium-list": [
            {
                "position": "1",
                "format": "Digital Media",
                "track-count": "1",
                "track-list": [
                    {
                        "position": "1",
                        "number": "1",
                        "length": "1000",
                        "recording": {"id": "demo-recording", "title": "LFO"},
                    }
                ],
            }
        ],
    }

demo.seed = seed
app = create_app()


def search_barcode(evidence):
    scenario = os.environ.get("HARMONIST_TEST_BARCODE_RESULTS", "2")
    if scenario == "error":
        raise mb_search.MBSearchError("MusicBrainz unavailable")
    count = int(scenario)
    return [
        mb_lookup.release_summary(demo.MB_RELEASES[mid]) for mid in (FIRST, SECOND)[:count]
    ], count


mb_search.search_barcode = search_barcode
