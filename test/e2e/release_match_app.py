"""Demo library with one release-match scenario per downloaded album (#618).

- Dingoes: matched to a label's digital release; another digital release links
  a different page on the artist's own store (the White Clouds case).
- Sex Bob-omb: another digital release links the download's exact store URL.
- Wyld Stallion: matched to a CD release, with an unlinked digital sibling.
"""

from copy import deepcopy
from typing import Any

from harmonist import demo
from harmonist.models import Release
from harmonist.web.main import create_app


def _sibling(mbid: str, of: str, **changes: Any) -> Release:
    release = deepcopy(demo.MB_RELEASES[of])
    release["id"] = mbid
    release.update(changes)
    demo.MB_RELEASES[mbid] = release
    return release


# Dingoes: the label's page on the current release, a renamed artist page on a
# shorter sibling, and a CD that stays out of the digital rows.
dingoes = "https://dingoes.bandcamp.com/album/little-bit-o-hoot"
demo.URL_RELS.pop(dingoes)
demo.URL_RELS["https://sunnydale-records.bandcamp.com/album/little-bit-o-hoot"] = "demo-rel-dingoes"
short = _sibling("demo-rel-dingoes-artist", "demo-rel-dingoes", date="2015-12-05")
short["medium-list"] = deepcopy(short["medium-list"])
short["medium-list"][0]["track-list"] = short["medium-list"][0]["track-list"][:2]
demo.URL_RELS["https://dingoes.bandcamp.com/album/dingoes-ate-my-baby-little-bit-o-hoot"] = (
    "demo-rel-dingoes-artist"
)
cd = _sibling("demo-rel-dingoes-cd", "demo-rel-dingoes", date="2001-05-01", country="US")
cd["medium-list"] = deepcopy(cd["medium-list"])
cd["medium-list"][0]["format"] = "CD"

# Sex Bob-omb: the download's URL sits on another digital release.
sex_bob_omb = "https://sexbobomb.bandcamp.com/album/we-are-here-to-make-you-sad"
demo.URL_RELS.pop(sex_bob_omb)
_sibling("demo-rel-sex-bob-omb-dl", "demo-rel-sex-bob-omb", disambiguation="Bandcamp download")
demo.URL_RELS[sex_bob_omb] = "demo-rel-sex-bob-omb-dl"
_sibling("demo-rel-sex-bob-omb-deluxe", "demo-rel-sex-bob-omb", disambiguation="deluxe")

# Wyld Stallion: matched to a CD; the digital release has no URL yet.
wyld = "https://wyldstallion.bandcamp.com/album/a-most-excellent-journey"
demo.URL_RELS.pop(wyld)
_sibling("demo-rel-wyld-digital", "demo-rel-wyld", date="2019-02-17")
demo.MB_RELEASES["demo-rel-wyld"]["medium-list"][0]["format"] = "CD"

app = create_app()
