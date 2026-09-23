"""A demo edition whose public download is tagged to a CD missing its URL."""

from harmonist import demo
from harmonist.web.main import create_app

demo.MB_RELEASES["demo-rel-dingoes"]["medium-list"][0]["format"] = "CD"
demo.URL_RELS.pop("https://dingoes.bandcamp.com/album/little-bit-o-hoot")
app = create_app()
