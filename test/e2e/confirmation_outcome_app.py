"""Release-review artwork with equal-size images and protected per-track art."""

import os

from test.e2e import contribution_app

from harmonist import demo, formats

app = contribution_app.app
SCENARIO = os.environ["HARMONIST_TEST_ARTWORK_OUTCOME"]
SELECTED = "demo-rel-dingoes-digital"
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
    original = (demo.ASSETS_DIR / "dingoes.png").read_bytes()
    other = (demo.ASSETS_DIR / "sonic.png").read_bytes()
    for index, path in enumerate(paths):
        formats.write_cover(path, other if SCENARIO.startswith("protected") and index else original)
    folder = paths[0].parent
    for name in ("cover.jpg", "cover.png"):
        (folder / name).unlink(missing_ok=True)
    if SCENARIO != "protected":
        (folder / "cover.png").write_bytes(
            (demo.ASSETS_DIR / "wyld.png").read_bytes() if SCENARIO == "tracks-only" else original
        )


def asset(release_mbid):
    if release_mbid == SELECTED and SCENARIO == "identical":
        return demo.ASSETS_DIR / "dingoes.png"
    return _asset(release_mbid)


demo.reset = reset
demo._archive_asset = asset
