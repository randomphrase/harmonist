"""Real mixed-quality demo files for the album-header regression and preview."""

import os
import shutil
from pathlib import Path

from pytest import MonkeyPatch
from test import demo_recovery

from harmonist import demo, formats
from harmonist.tagger import tagsets_for
from harmonist.transforms import TaggingChoices

assert os.environ.get("HARMONIST_DEMO_MODE") == "1"
patch = MonkeyPatch()
demo_recovery.install(patch)
ALBUM_ID = "demo-rel-electric-mayhem"
release = demo.MB_RELEASES[ALBUM_ID]
library = [
    spec | {"tracks": ["Can You Picture That?", "Mahna Mahna", "Movin' Right Along"]}
    if spec.get("file_mbid") == ALBUM_ID
    else spec
    for spec in demo.LIBRARY
]
patch.setattr(demo, "LIBRARY", library)
materialise = demo._materialise


def mixed_formats(music_dir, spec):
    materialise(music_dir, spec)
    if spec.get("file_mbid") != ALBUM_ID:
        return
    folder = music_dir / demo_recovery._safe(spec["artist"]) / demo_recovery._safe(spec["album"])
    tagsets = tagsets_for(release, TaggingChoices())
    for i, source in enumerate(sorted(folder.glob("*.m4a"))):
        fixture = "sine-hires.flac" if i == 2 else "sine.flac"
        target = source.with_suffix(".flac")
        shutil.copy(Path(__file__).parents[1] / "fixtures" / fixture, target)
        cover = formats.read_cover(source)
        formats.write_tags(target, tagsets[i], cover[0] if cover else None)
        source.unlink()


patch.setattr(demo, "_materialise", mixed_formats)
from harmonist.web import main

app = main.app
