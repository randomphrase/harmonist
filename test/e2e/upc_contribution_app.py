"""A Qobuz-style download tagged as a CD, with two digital sibling choices."""

from test.test_gardener import _release
from test.test_upc_contributions import MBID, UPC, download

from harmonist import demo
from harmonist.web.main import create_app

_seed = demo.seed


def seed(music_dir, **kwargs):
    _seed(music_dir, **kwargs)
    download(music_dir)


for mbid, description in ((MBID, "CD"), ("upc-digital", "Download"), ("upc-unlinked", "Reissue")):
    payload = _release(mbid=mbid)
    payload["disambiguation"] = description
    if mbid == MBID:
        payload["medium-list"][0]["format"] = "CD"
    if mbid == "upc-digital":
        payload["barcode"] = UPC[1:]
    demo.MB_RELEASES[mbid] = payload

demo.seed = seed
app = create_app()
