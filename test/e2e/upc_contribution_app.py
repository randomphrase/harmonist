"""A Qobuz-style download tagged as a CD, with two digital sibling choices."""

import os

from test.test_gardener import _release
from test.test_upc_contributions import MBID, OTHER_UPC, UPC, download

from harmonist import demo
from harmonist.web.main import create_app

_seed = demo.seed
barcode_scenario = os.environ.get("HARMONIST_TEST_UPC_CONTRIBUTION")


def seed(music_dir, **kwargs):
    _seed(music_dir, **kwargs)
    download(music_dir)


for mbid, description in ((MBID, "CD"), ("upc-digital", "Download"), ("upc-unlinked", "Reissue")):
    payload = _release(mbid=mbid)
    payload["disambiguation"] = description
    if mbid == MBID and not barcode_scenario:
        payload["medium-list"][0]["format"] = "CD"
    if mbid == "upc-digital":
        payload["barcode"] = OTHER_UPC if barcode_scenario == "absent" else UPC[1:]
    demo.MB_RELEASES[mbid] = payload

demo.seed = seed
app = create_app()
