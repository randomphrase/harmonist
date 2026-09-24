"""Real artwork routes with images larger than the browser's viewport."""

from pathlib import Path
from tempfile import gettempdir

from test.e2e import contribution_app
from test.test_artwork import png_bytes

from harmonist import demo, formats

app = contribution_app.app
_reset = demo.reset
_asset = demo._archive_asset


def reset(music_dir, **kwargs):
    _reset(music_dir, **kwargs)
    paths = sorted(
        p
        for p in music_dir.rglob("*")
        if formats.is_supported(p) and formats.read_album_id(p) == "demo-rel-dingoes"
    )
    assert len(paths) == 3
    for path in paths:
        formats.write_cover(path, png_bytes(1600, 1000))
    (paths[0].parent / "cover.jpg").unlink(missing_ok=True)
    (paths[0].parent / "cover.png").write_bytes(png_bytes(2400, 1600))


def asset(release_mbid):
    if release_mbid == "demo-rel-dingoes-digital":
        path = Path(gettempdir()) / "inspection-candidate.png"
        path.write_bytes(png_bytes(2000, 1200))
        return path
    return _asset(release_mbid)


demo.reset = reset
demo._archive_asset = asset
