#!/usr/bin/env python3
"""Read-only dry-run of the slug-match item_id backfill.

Fetches your Bandcamp collection (authenticated, READ-ONLY — no downloads, no
sidecar writes), constructs each item's URL, and reports which on-disk
NEEDS_SYNC albums the slug fallback (`find_existing_album_by_slug`) would link.

It also dumps the fetched collection to a JSON file so the live result can be
frozen into a regression fixture without needing the network again.

Nothing is mutated: it never calls write_sidecar_for_item, never downloads,
never touches ignores.txt or the checkpoint.

Usage (run from the repo root so it uses your real config):

    python scripts/slug_match_dryrun.py
    python scripts/slug_match_dryrun.py --dump /tmp/collection.json

Because it makes an authenticated network call to Bandcamp, prefer running it
yourself (e.g. `! python scripts/slug_match_dryrun.py` inside the session) so
it uses your environment directly.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from harmonist import config as config_mod
from harmonist import sidecar as sidecar_mod
from harmonist.bandcamp_hook import album_slug, construct_bandcamp_url
from harmonist.models import AlbumState
from harmonist.scanner import scan


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dump",
        type=Path,
        default=None,
        help="write the fetched collection (band/title/id/url/slug) to this JSON file",
    )
    args = ap.parse_args()

    cfg = config_mod.load()
    music_dir = cfg.paths.music_dir
    print(f"music_dir : {music_dir}")
    print(f"cookies   : {cfg.cookies_file}")

    # --- on-disk NEEDS_SYNC albums (have store_url + MBID, no item_id) ---
    albums = scan(music_dir)
    needs_sync = [a for a in albums if a.state == AlbumState.NEEDS_SYNC]
    by_slug: dict[str, list] = {}
    for a in needs_sync:
        s = album_slug(a.sidecar.store_url if a.sidecar else None)
        if s:
            by_slug.setdefault(s, []).append(a)
    print(f"\nNEEDS_SYNC albums: {len(needs_sync)} ({len(by_slug)} with a usable slug)")

    # --- fetch the collection (read-only) ---
    if not cfg.cookies_file.exists():
        print(f"\nERROR: no cookies file at {cfg.cookies_file}")
        return 2
    cookies = cfg.cookies_file.read_text(encoding="utf-8")

    from bandcampsync.bandcamp import Bandcamp  # imported here so --help needs no network deps

    print("\nfetching collection (read-only)…")
    bc = Bandcamp(cookies=cookies)
    bc.verify_authentication()
    bc.load_purchases()  # stop_when=None → full collection (ignores the checkpoint)
    items = list(bc.purchases)
    print(f"collection items: {len(items)}")

    # --- simulate the slug match ---
    dump = []
    would_link: list[tuple] = []
    ambiguous: list[tuple] = []
    for item in items:
        url = construct_bandcamp_url(item)
        s = album_slug(url)
        dump.append(
            {
                "band": getattr(item, "band_name", None),
                "title": getattr(item, "item_title", None),
                "item_id": getattr(item, "item_id", None),
                "url": url,
                "slug": s,
            }
        )
        if not s:
            continue
        candidates = by_slug.get(s, [])
        if len(candidates) == 1:
            would_link.append((item, candidates[0], url))
        elif len(candidates) > 1:
            ambiguous.append((item, candidates, url))

    linked_slugs = {album_slug(construct_bandcamp_url(i)) for i, _, _ in would_link}

    print(f"\n=== WOULD LINK ({len(would_link)}) ===")
    for item, album, url in sorted(would_link, key=lambda t: t[2] or ""):
        print(f"  ✓ {album.artist} / {album.title}")
        print(f"      disk : {album.sidecar.store_url}")
        print(f"      item : {url}  (id:{getattr(item, 'item_id', '?')})")

    if ambiguous:
        print(f"\n=== AMBIGUOUS — skipped ({len(ambiguous)}) ===")
        for item, albums_, url in ambiguous:
            print(f"  ? {url}: {len(albums_)} disk albums share this slug")

    unmatched = [
        a for a in needs_sync if album_slug(a.sidecar.store_url if a.sidecar else None) not in linked_slugs
    ]
    print(f"\n=== STILL UNMATCHED ({len(unmatched)}) ===")
    for a in unmatched:
        print(f"  ✗ {a.artist} / {a.title}")
        print(f"      disk slug: {album_slug(a.sidecar.store_url) if a.sidecar else None}")

    if args.dump:
        args.dump.write_text(json.dumps(dump, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nwrote collection dump → {args.dump}  ({len(dump)} items)")

    print("\n(dry-run — nothing was written, downloaded, or ignored)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
