"""The sidecar's record of stand-in covers (#663): how it is kept on disk.

What writes it and what it changes on the album page are `test_web`'s; here is
the published-interface half — what the file holds, what an older or damaged
file reads as, and that a change is audited.
"""

from __future__ import annotations

import json
import logging

from harmonist import sidecar as sidecar_mod
from harmonist.models import BorrowedArtwork, Sidecar

MBID = "rel-1"
GROUP_COVER = BorrowedArtwork(
    digest="ab" * 32, source="release-group", source_mbid="rg-1", release=MBID, image_id="9"
)
SIBLING_COVER = BorrowedArtwork(
    digest="cd" * 32, source="release", source_mbid="rel-uk", release=MBID
)


def test_stand_ins_round_trip_and_none_is_omitted(tmp_path):
    held, none = tmp_path / "held", tmp_path / "none"
    for d, borrowed in ((held, (GROUP_COVER, SIBLING_COVER)), (none, ())):
        d.mkdir()
        sidecar_mod.write(d, Sidecar(mb_release_id=MBID, borrowed_artwork=borrowed))
        assert sidecar_mod.read(d).borrowed_artwork == borrowed

    written = json.loads(sidecar_mod.sidecar_path(held).read_text())
    assert written["borrowed_artwork"] == [
        {
            "digest": GROUP_COVER.digest,
            "source": "release-group",
            "source_mbid": "rg-1",
            "release": MBID,
            "image_id": "9",
        },
        # No image id, so none written: absent, like every other default.
        {
            "digest": SIBLING_COVER.digest,
            "source": "release",
            "source_mbid": "rel-uk",
            "release": MBID,
        },
    ]
    assert "borrowed_artwork" not in json.loads(sidecar_mod.sidecar_path(none).read_text())


def test_a_sidecar_from_before_stand_ins_reads_as_having_none(tmp_path):
    sidecar_mod.sidecar_path(tmp_path).write_text(
        json.dumps({"schema_version": 1, "mb_release_id": MBID})
    )

    assert sidecar_mod.read(tmp_path).borrowed_artwork == ()


def test_a_damaged_entry_is_dropped_not_the_album(tmp_path):
    """The record only ever suggests artwork: losing an entry costs a
    suggestion, while refusing the sidecar would hide the album."""
    good = {"digest": "ab" * 32, "source": "release-group", "source_mbid": "rg-1", "release": MBID}
    sidecar_mod.sidecar_path(tmp_path).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mb_release_id": MBID,
                "borrowed_artwork": [
                    good,
                    {"digest": "cd" * 32},
                    "nonsense",
                    {**good, "release": 7},
                ],
            }
        )
    )

    assert sidecar_mod.read(tmp_path).borrowed_artwork == (
        BorrowedArtwork(digest="ab" * 32, source="release-group", source_mbid="rg-1", release=MBID),
    )


def test_an_albums_stand_ins_are_every_parts_for_its_release():
    """A cover borrowed onto one disc's folder is the album's; one recorded for
    some other release says nothing about this one."""
    from dataclasses import replace

    from harmonist.scanner import _merge_sidecars

    elsewhere = replace(SIBLING_COVER, release="rel-other")
    merged = _merge_sidecars(
        [
            Sidecar(mb_release_id=MBID, borrowed_artwork=(GROUP_COVER,)),
            Sidecar(mb_release_id=MBID, borrowed_artwork=(GROUP_COVER, elsewhere)),
            None,
        ],
        MBID,
    )

    assert merged.borrowed_artwork == (GROUP_COVER,)


def test_a_change_to_the_stand_ins_is_audited(tmp_path, caplog):
    """Provenance nothing else records, so a change is recorded as it happens."""
    sidecar_mod.write(tmp_path, Sidecar(mb_release_id=MBID))
    with caplog.at_level(logging.INFO, logger="harmonist.audit"):
        sidecar_mod.write(tmp_path, Sidecar(mb_release_id=MBID, borrowed_artwork=(GROUP_COVER,)))
        sidecar_mod.write(tmp_path, Sidecar(mb_release_id=MBID, borrowed_artwork=(GROUP_COVER,)))

    updates = [
        r.getMessage()
        for r in caplog.records
        if r.name == "harmonist.audit" and r.getMessage().startswith("sidecar.update")
    ]
    # Once: the second write changed nothing, and says nothing.
    assert len(updates) == 1
    assert updates[0].endswith(f" borrowed_artwork=[]->[{'ab' * 6}@release-group]")
