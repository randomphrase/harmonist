"""A sidecar rewrite must not silently drop the fields it wasn't told about (#263).

Twelve places rebuilt a `Sidecar` from an existing one by naming the fields to
keep. Every field left off the list was reset to its default — including
`purchase_unavailable` and `tracks_unavailable`, decisions the user recorded that
have no evidence on disk and so cannot be recovered once lost.

#239 diagnosed exactly this and fixed one site. Its comment is still in
`_tag_with_release`, and its last sentence is why this file exists:

    A field added to the model later would have joined them, silently, with no
    test to notice.

So this is that test. It is a source check rather than a behavioural one on
purpose: the behavioural version needs one case per site per field, and would
still say nothing about the *next* site someone writes.
"""

from __future__ import annotations

import ast
from dataclasses import fields as dc_fields
from pathlib import Path

import pytest

from harmonist.models import Sidecar

SRC = Path(__file__).parent.parent / "src" / "harmonist"

#: Names a construction site uses for the sidecar it is carrying forward. A
#: `Sidecar(...)` call mentioning one of these is rebuilding an existing sidecar
#: rather than minting a fresh one, and is therefore in scope here.
CARRIER_PREFIXES = ("sc.", "existing.", "base.", "s.", "cached.")

#: `_merge_sidecars` is the one site that legitimately spells every field out:
#: merging N sidecars needs a rule per field (first / earliest / latest / any),
#: which `replace()` off some arbitrary part cannot express. It is allowed to
#: construct, but not to be incomplete — `test_the_merge_names_every_field`
#: holds it total.
EXPLICIT_BY_DESIGN = {("scanner.py", "_merge_sidecars")}


def _sidecar_calls() -> list[tuple[Path, ast.Call, str, str | None]]:
    """Every `Sidecar(...)` construction in `src/`, with its enclosing function."""
    out = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "demo.py":  # a fixture generator, not a rewrite path
            continue
        src = path.read_text()
        tree = ast.parse(src)
        enclosing: dict[int, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                for child in ast.walk(node):
                    enclosing.setdefault(getattr(child, "lineno", -1), node.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Sidecar":
                seg = ast.get_source_segment(src, node) or ""
                out.append((path, node, seg, enclosing.get(node.lineno)))
    return out


def test_no_sidecar_rewrite_names_the_fields_to_keep():
    """A site that carries an existing sidecar forward must use `replace()`.

    `replace(base, ...)` keeps every unnamed field BY CONSTRUCTION, so a field
    added to the model tomorrow needs no edit here and cannot be dropped.
    """
    offenders = []
    for path, node, seg, func in _sidecar_calls():
        if not node.keywords or not any(p in seg for p in CARRIER_PREFIXES):
            continue
        if (path.name, func) in EXPLICIT_BY_DESIGN:
            continue
        offenders.append(f"{path.relative_to(SRC.parent.parent)}:{node.lineno} in {func}()")

    assert not offenders, (
        "these rebuild a Sidecar from an existing one by naming fields to keep, "
        "so any field they omit is silently reset to its default — use "
        "`dataclasses.replace(sc, ...)` instead:\n  " + "\n  ".join(offenders)
    )


def test_the_merge_names_every_field():
    """The one site allowed to construct explicitly must name every field.

    `_merge_sidecars` builds the album's view of its folders' sidecars (#197).
    A field it forgets is defaulted on every multi-folder album — which is how
    `video_media` came to be dropped there, costing those albums #206 and one
    MusicBrainz request per scan to re-learn it.
    """
    named: set[str] = set()
    for path, node, _seg, func in _sidecar_calls():
        if (path.name, func) in EXPLICIT_BY_DESIGN:
            named = {k.arg for k in node.keywords if k.arg}
    assert named, "_merge_sidecars not found — has it been renamed or moved?"

    expected = {f.name for f in dc_fields(Sidecar)} - {"schema_version"}
    assert expected - named == set(), (
        "_merge_sidecars does not say what to do with these fields, so they are "
        f"defaulted on every multi-folder album: {sorted(expected - named)}"
    )


def test_linking_a_purchase_clears_the_surrender(tmp_path):
    """Carrying fields forward must not carry one its own evidence refutes.

    `purchase_unavailable` says "there is no purchase to link, ever". Linking a
    purchase does exactly that, so it is cleared — deliberately, which is a
    different act from the accidental defaulting the rest of #263 is about.
    Without it, converting this path to `replace()` would leave the album
    flagged as having no purchase while wearing the item id of the one it just
    got, and the flag would go on suppressing Needs Link forever.
    """
    import shutil

    from harmonist import sidecar as sidecar_mod
    from harmonist.pending_downloads import PendingPurchase
    from harmonist.scanner import scan
    from harmonist.web.main import _link_pending_to_album

    album_dir = tmp_path / "Artist" / "Surrendered"
    album_dir.mkdir(parents=True)
    shutil.copy(Path(__file__).parent / "fixtures" / "sine.m4a", album_dir / "01 Track.m4a")
    sidecar_mod.write(
        album_dir,
        Sidecar(
            store_url="https://x.bandcamp.com/album/surrendered",
            purchase_unavailable=True,
        ),
    )
    album = next(a for a in scan(tmp_path) if a.path == album_dir)

    _link_pending_to_album(
        album,
        PendingPurchase(
            item_id=4242,
            band="X",
            title="Surrendered",
            url="https://x.bandcamp.com/album/surrendered",
            fmt="flac",
        ),
    )

    after = sidecar_mod.read(album_dir)
    assert after is not None
    assert after.bandcamp is not None and after.bandcamp.item_id == 4242
    assert after.purchase_unavailable is False, (
        "the purchase turned out to exist, so the no-purchase surrender is refuted"
    )


@pytest.mark.parametrize("field", [f.name for f in dc_fields(Sidecar)])
def test_every_sidecar_field_survives_a_replace(field):
    """The property the fix relies on, stated once rather than assumed.

    Guards against a field being given a non-init default or otherwise dropping
    out of `replace()`, which would make every converted site quietly wrong
    again while both tests above stayed green.
    """
    from dataclasses import replace

    markers: dict[str, object] = {
        "schema_version": 99,
        "store_url": "https://example.bandcamp.com/album/x",
        "notes": "note",
        "purchase_unavailable": True,
        "tracks_unavailable": True,
        "video_media": (1, 2),
        "temp_uid": "uid",
        "mb_release_id": "11111111-2222-3333-4444-555555555555",
    }
    if field not in markers:
        pytest.skip(f"{field} has no simple distinguishable value")
    marker = markers[field]

    # `setattr` rather than a `**kwargs` splat: the field name is only known at
    # runtime, so a splat cannot be typed against the constructor.
    original = Sidecar()
    setattr(original, field, marker)
    assert getattr(replace(original, added_at=None), field) == marker
