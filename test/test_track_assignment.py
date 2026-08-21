"""Which file is which MusicBrainz track (#232).

*TISM — The White Albun* is three media: **DVD-Video 22 · CD 16 · DVD-Video 31**.
The CD's sixteen tracks are on disk and always have been. They were tagged when
MusicBrainz's release held only the CD — so they say disc 1 — and in May 2026 the
two DVDs were added and the CD moved to position 2.

The album page then paired all sixteen against disc 1's *videos*: "22 of 22
tracks differ from MusicBrainz · Disc 2, Disc 3 not on disk", with a complete CD
reported as a disc that isn't there.

Every one of those files carries a `MusicBrainz Release Track Id`, which names
one position in one release and cannot be ambiguous. Harmonist writes it on
everything it tags, Picard writes the same — and Picard used it to re-file the
album without being asked. Harmonist read it for a different question (#197) and
never for this one, deciding instead from numbers MusicBrainz had just
renumbered, and — in the tagger — from durations.

So the ladder, best rung first: release track id, disc-and-track number, file
order. No length similarity: a duration is not an identity.
"""

from __future__ import annotations

import pytest

from harmonist.compare import (
    Agreement,
    MBTrack,
    Medium,
    TrackIdentity,
    TrackState,
    assign,
    identity_of,
)
from harmonist.compare import tracklist as compare_tracklist
from harmonist.formats.types import TagSet, TrackTags

# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


def _mb(disc: int, track: int, rtid: str | None = None) -> TrackIdentity:
    return TrackIdentity(rtid or f"rt-{disc}-{track}", disc, track)


def test_a_file_naming_its_slot_is_that_slot():
    """The rung that is not a guess."""
    tracks = [_mb(1, 1), _mb(1, 2), _mb(1, 3)]
    files = [TrackIdentity("rt-1-3"), TrackIdentity("rt-1-1")]

    assert assign(files, tracks) == [2, 0]


def test_the_id_beats_a_number_that_disagrees_with_it():
    """The TISM shape: the numbers are stale because MusicBrainz renumbered the
    release; the id in the same file is not. Nothing else can tell them apart —
    both are tags, both look equally authoritative, and only one of them means
    anything outside this release's current numbering."""
    tracks = [_mb(1, 1), _mb(1, 2)]
    files = [TrackIdentity("rt-1-2", disc=1, track=1)]

    assert assign(files, tracks) == [1]


def test_a_number_still_answers_for_a_file_with_no_id():
    """An adopted album Harmonist has never tagged carries no ids at all, and
    the numbers are all it has. This is the rule as it always was."""
    tracks = [_mb(1, 1), _mb(1, 2), _mb(1, 3)]
    files = [TrackIdentity(disc=1, track=3), TrackIdentity(disc=1, track=1)]

    assert assign(files, tracks) == [2, 0]


def test_a_number_is_paired_with_its_disc():
    """Track 4 exists on both halves of a 2-CD release."""
    tracks = [_mb(1, 1), _mb(1, 2), _mb(2, 1), _mb(2, 2)]
    files = [TrackIdentity(disc=2, track=1)]

    assert assign(files, tracks) == [2]


def test_files_with_nothing_to_say_are_dealt_in_order():
    """An album with no ids and no numbers behaves exactly as positional pairing
    always did — the fallback, not a rung."""
    tracks = [_mb(1, 1), _mb(1, 2), _mb(1, 3)]
    files = [TrackIdentity(), TrackIdentity()]

    assert assign(files, tracks) == [0, 1]


def test_a_claim_two_files_make_settles_nothing():
    """Two files naming one id are two copies of a track, or a mis-tag. Either
    way the claim is not unique, so it decides nothing and both fall through."""
    tracks = [_mb(1, 1), _mb(1, 2)]
    files = [TrackIdentity("rt-1-2"), TrackIdentity("rt-1-2")]

    # Dealt in file order into the free slots, not both fighting over slot 1.
    assert assign(files, tracks) == [0, 1]


def test_a_slot_musicbrainz_names_twice_is_no_slot():
    """Ambiguity on MusicBrainz's side is dropped rather than resolved to the
    first holder."""
    tracks = [TrackIdentity("dup", 1, 1), TrackIdentity("dup", 1, 2)]
    files = [TrackIdentity("dup")]

    assert assign(files, tracks) == [0], "falls through to file order, not to 'the first dup'"


def test_an_id_for_a_track_this_release_does_not_have_falls_through():
    """A file re-matched to a different release keeps the old release's ids
    until it is re-tagged. They name nothing here."""
    tracks = [_mb(1, 1), _mb(1, 2)]
    files = [TrackIdentity("rt-from-another-release", disc=1, track=2)]

    assert assign(files, tracks) == [1], "the number still answers"


def test_every_file_is_tried_on_a_rung_before_the_next_rung_is_tried():
    """One file's missing id must not cost another file its own. If the rungs
    ran per-file, the numbered file would take slot 0 first and the file that
    NAMES slot 0 would be pushed off it."""
    tracks = [_mb(1, 1), _mb(1, 2)]
    files = [TrackIdentity(disc=1, track=1), TrackIdentity("rt-1-1")]

    assert assign(files, tracks) == [1, 0]


def test_an_unreadable_file_claims_nothing():
    """It has no tags to read (#112) — which is not the same as a readable file
    that carries no numbers, and must not be given (disc 1, track None)."""
    assert identity_of(TrackTags(unreadable=True, track_num=3)) == TrackIdentity()


def test_more_files_than_tracks_leaves_the_extras_unassigned():
    tracks = [_mb(1, 1)]
    files = [TrackIdentity("rt-1-1"), TrackIdentity(), TrackIdentity()]

    assert assign(files, tracks) == [0, None, None]


# ---------------------------------------------------------------------------
# The album page, on the release that started it
# ---------------------------------------------------------------------------


def _tism_release() -> list[MBTrack]:
    """DVD-Video 22 · CD 16 · DVD-Video 31, as MusicBrainz has it today."""
    out = []
    for disc, count, kind in ((1, 22, "Video"), (2, 16, "Song"), (3, 31, "Extra")):
        for n in range(1, count + 1):
            out.append(
                MBTrack(
                    tags=TagSet(
                        title=f"{kind} {n}",
                        album="The White Albun",
                        artist="TISM",
                        mb_album_id="rel-tism",
                        album_artist="TISM",
                        track_total=count,
                        mb_release_track_id=f"rt-{disc}-{n}",
                        disc_num=disc,
                        track_num=n,
                    ),
                    length_ms=200_000,
                )
            )
    return out


def _tism_media() -> list[Medium]:
    return [Medium(1, None, "DVD-Video"), Medium(2, None, "CD"), Medium(3, None, "DVD-Video")]


def _tism_files(*, stale_disc: bool) -> list[tuple[str, TrackTags]]:
    """The sixteen CD files. `stale_disc` is the state they were in before the
    re-tag: correct ids, and a disc number from when the CD was disc 1."""
    return [
        (
            f"2-{n:02d} Song {n}.m4a",
            TrackTags(
                title=f"Song {n}",
                artist="TISM",
                release_track_id=f"rt-2-{n}",
                disc_num=1 if stale_disc else 2,
                track_num=n,
                duration_ms=200_000,
            ),
        )
        for n in range(1, 17)
    ]


@pytest.mark.parametrize("stale_disc", [True, False])
def test_the_cd_is_the_cd_however_musicbrainz_has_renumbered_it(stale_disc):
    """The reported bug. With the stale disc number the whole CD used to key
    onto disc 1's videos; the ids in the same files said medium 2 all along."""
    t = compare_tracklist(_tism_files(stale_disc=stale_disc), _tism_release(), _tism_media())
    cd = t.discs[1]

    assert cd.medium.position == 2
    assert cd.absent is False, "a complete CD is not a disc that isn't on disk"
    assert [r.state for r in cd.tracks] == [TrackState.PRESENT] * 16
    assert [r.fields[1].disk for r in cd.tracks] == [f"Song {n}" for n in range(1, 17)]
    assert [r.fields[1].mb for r in cd.tracks] == [f"Song {n}" for n in range(1, 17)]


def test_a_stale_disc_number_is_reported_as_the_difference_it_is():
    """What the page should have been saying all along: these are the right
    tracks, and one tag on them has gone out of date. The disc number is a
    finding about the file — not the thing that decides which track it is."""
    t = compare_tracklist(_tism_files(stale_disc=True), _tism_release(), _tism_media())
    row = t.discs[1].tracks[4]

    number, title = row.fields[0], row.fields[1]
    assert (number.disk, number.mb) == ("1-5", "2-5"), "the tag that is stale"
    assert title.agreement is Agreement.MATCHES, "the tags that aren't"
    assert [f.label for f in row.fields if f.differs] == ["#"]


def test_a_re_tagged_album_then_agrees_completely():
    """The state the album is in once the correction is written back. The two
    DVDs are still missing, which is a fact about the library and not about
    these files."""
    t = compare_tracklist(_tism_files(stale_disc=False), _tism_release(), _tism_media())

    assert [r for r in t.discs[1].tracks if r.differs] == []
    assert t.summary == "All 16 tracks match MusicBrainz · Disc 1, Disc 3 not on disk"


def test_the_videos_are_still_reported_as_absent_discs():
    """The other half of the same page: the two DVDs genuinely aren't on disk,
    and #216 collapses each into one line. That must survive the re-assignment."""
    t = compare_tracklist(_tism_files(stale_disc=True), _tism_release(), _tism_media())

    assert [g.absent for g in t.discs] == [True, False, True]
    assert "Disc 1, Disc 3 not on disk" in t.summary
