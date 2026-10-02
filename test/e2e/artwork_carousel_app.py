"""An archive that lists a front, a back and another front for every release
(#659): the carousel has images to step between, and Front only has a back
cover to hide."""

from test.e2e import demo_app

from harmonist import cover_art, demo

app = demo_app.app

#: (image id, types) per listed image, in the archive's order.
IMAGES = (("101", ("Front",)), ("102", ("Back",)), ("103", ("Front",)))


def fetch_listing(kind, mbid, *, client=None):
    if kind != "release":
        return []
    return [
        cover_art.Candidate(
            image_id=image_id,
            image_url=f"https://coverartarchive.org/release/{mbid}/{image_id}.jpg",
            thumbnail_url=f"https://coverartarchive.org/release/{mbid}/{image_id}-500.jpg",
            types=types,
            front="Front" in types,
        )
        for image_id, types in IMAGES
    ]


def fetch_bytes(url, *, client=None):
    # Distinct bytes per image, so no two are deduplicated as one picture.
    data = (demo.ASSETS_DIR / "cover-3.jpg").read_bytes() + url.encode()
    return data, "image/jpeg"


cover_art.fetch_listing = fetch_listing
cover_art.fetch_bytes = fetch_bytes
