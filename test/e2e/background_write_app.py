"""A background pass writing into an album while its page is open (#975).

`POST /test/background-write/{album_id}` fills every track that has no image
with the image its neighbours carry, then refreshes the album as the apply pass
does after each album it writes (`_refresher`): the snapshot is replaced and
the scan sequence advances, which is all an open page hears about. The test
sends it rather than the page, so it is nobody's action on the page.
"""

from fastapi import FastAPI
from test.e2e import demo_app

from harmonist import formats, scanner

app: FastAPI = demo_app.app


@app.post("/test/background-write/{album_id}")
def background_write(album_id: str) -> dict[str, int]:
    runner = app.state.scan_runner
    album = next(a for a in runner.albums() if a.id == album_id)
    tracks = [f for d in album.folders if (c := scanner.read_dir(d)) for f in c.files]
    covers = [formats.read_cover(f) for f in tracks]
    image = next(c[0] for c in covers if c is not None)
    written = 0
    for track, cover in zip(tracks, covers, strict=True):
        if cover is None:
            formats.write_cover(track, image)
            written += 1
    runner.refresh_album(album.folders, notify=True)
    return {"written": written}
