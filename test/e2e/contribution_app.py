"""A demo edition whose public download is tagged to a CD missing its URL."""

from copy import deepcopy

from harmonist import demo
from harmonist.web.main import create_app

demo.MB_RELEASES["demo-rel-dingoes"]["medium-list"][0]["format"] = "CD"
demo.URL_RELS.pop("https://dingoes.bandcamp.com/album/little-bit-o-hoot")
for mbid, description in (
    ("demo-rel-dingoes-digital", "Bandcamp download"),
    ("demo-rel-dingoes-reissue", "Digital reissue"),
):
    edition = deepcopy(demo.MB_RELEASES["demo-rel-dingoes"])
    edition["id"] = mbid
    edition["disambiguation"] = description
    edition["medium-list"][0]["format"] = "Digital Media"
    demo.MB_RELEASES[mbid] = edition
demo.URL_RELS["https://dingoes.bandcamp.com/album/little-bit-o-hoot"] = "demo-rel-dingoes-digital"
app = create_app()
