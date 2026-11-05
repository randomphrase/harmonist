"""An NFO that deliberately disagrees with the stored MusicBrainz artist."""

import os
from datetime import UTC, datetime

from test.e2e import artists_app
from test.test_artist_web import ALICE, RELEASE

from harmonist import activity_store, demo, mb_lookup

app = artists_app.app
_reset = demo.reset


def identity(music_dir):
    nfo = music_dir / "ArtistInfo" / "unrelated directory name" / "artist.nfo"
    nfo.write_text(
        nfo.read_text()
        .replace("<name>Álice</name>", "<name>Local Álice</name>")
        .replace(
            "</artist>", "<disambiguation>local project</disambiguation><mood>Calm</mood></artist>"
        )
    )
    stamp = datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC).timestamp()
    os.utime(nfo, (stamp, stamp))
    activity_store.store_release(
        RELEASE,
        "+".join(sorted(mb_lookup.RELEASE_INCLUDES)),
        {
            "id": RELEASE,
            "artist-credit": [
                {
                    "name": "Credited alias",
                    "artist": {
                        "id": ALICE,
                        "name": "Canonical Álice",
                        "disambiguation": "British singer",
                    },
                }
            ],
        },
    )


def reset(music_dir, **kwargs):
    _reset(music_dir, **kwargs)
    identity(music_dir)


demo.reset = reset
identity(app.state.cfg.paths.music_dir)
