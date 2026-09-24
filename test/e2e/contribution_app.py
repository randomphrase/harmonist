"""A demo edition whose public download is tagged to a CD missing its URL."""

from copy import deepcopy
from pathlib import Path

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

_original_archive_asset = demo._archive_asset


def _edition_artwork(release_mbid: str) -> Path | None:
    if release_mbid in {"demo-rel-dingoes-digital", "demo-rel-dingoes-reissue"}:
        # A different cover catches previews accidentally served under the
        # current release's ID, even though their metadata loaded correctly.
        return demo.ASSETS_DIR / "wyld.png"
    return _original_archive_asset(release_mbid)


demo._archive_asset = _edition_artwork
app = create_app()
