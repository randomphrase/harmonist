"""Tests for the Picard-compatible tagger."""

from __future__ import annotations

import hashlib
import os

import pytest
from mutagen.mp4 import MP4, MP4Cover

from harmonist import tagger
from harmonist.formats import owned
from harmonist.tagger import (
    ATOM_ALBUM,
    ATOM_ALBUM_ARTIST,
    ATOM_ALBUM_ARTIST_SORT,
    ATOM_ARTIST,
    ATOM_ARTIST_SORT,
    ATOM_ARTISTS,
    ATOM_ASIN,
    ATOM_BARCODE,
    ATOM_CATALOG,
    ATOM_COMMENT,
    ATOM_COVER,
    ATOM_DATE,
    ATOM_DISC_NUM,
    ATOM_ISRC,
    ATOM_LABEL,
    ATOM_MB_ALBUM_ARTIST_ID,
    ATOM_MB_ALBUM_COUNTRY,
    ATOM_MB_ALBUM_ID,
    ATOM_MB_ALBUM_STATUS,
    ATOM_MB_ALBUM_TYPE,
    ATOM_MB_ARTIST_ID,
    ATOM_MB_RELEASE_GROUP_ID,
    ATOM_MB_RELEASE_TRACK_ID,
    ATOM_MB_TRACK_ID,
    ATOM_MEDIA,
    ATOM_ORIGINAL_DATE,
    ATOM_ORIGINAL_YEAR,
    ATOM_SCRIPT,
    ATOM_TITLE,
    ATOM_TRACK_NUM,
    LEGACY_RELEASE_ID,
    TagMismatchError,
)


def _release_2_tracks() -> dict:
    """Build a synthetic 2-track MB release dict with all the trimmings."""
    return {
        "id": "rel-aaa",
        "title": "Test Album",
        "status": "Official",
        "country": "GB",
        "date": "2021-06-15",
        "barcode": "0123456789012",
        "asin": "B00ASIN1234",
        "text-representation": {"language": "eng", "script": "Latn"},
        "artist-credit": [
            {
                "artist": {
                    "id": "art-aaa",
                    "name": "Test Artist",
                    "sort-name": "Test Artist, The",
                },
                "name": "Test Artist",
            },
        ],
        "release-group": {
            "id": "rg-aaa",
            "primary-type": "Album",
            "first-release-date": "2019-01-01",
        },
        "label-info-list": [
            {"label": {"name": "Test Label"}, "catalog-number": "CAT-001"},
        ],
        "medium-list": [
            {
                "position": "1",
                "format": "Digital Media",
                "track-list": [
                    {
                        "id": "rt-001",
                        "position": "1",
                        "title": "Track 1",
                        "recording": {
                            "id": "rec-001",
                            "title": "Track 1",
                            "isrc-list": ["GBTEST2100001"],
                        },
                    },
                    {
                        "id": "rt-002",
                        "position": "2",
                        "title": "Track 2",
                        "recording": {"id": "rec-002", "title": "Track 2"},
                        "artist-credit": [
                            {
                                "artist": {
                                    "id": "art-bbb",
                                    "name": "Featured Artist",
                                    "sort-name": "Featured Artist",
                                },
                                "name": "Featured Artist",
                            },
                            # musicbrainzngs emits join phrases as bare strings.
                            " feat. ",
                            {
                                "artist": {
                                    "id": "art-ccc",
                                    "name": "Other",
                                    "sort-name": "Other, The",
                                },
                                "name": "Other",
                            },
                        ],
                    },
                ],
            }
        ],
    }


def _atom_str(audio: MP4, atom: str) -> str:
    return audio[atom][0].decode("utf-8")


def _atom_strs(audio: MP4, atom: str) -> list[str]:
    return [v.decode("utf-8") for v in audio[atom]]


def test_tag_album_writes_audit_records(album_with_tracks, tmp_path):
    """Tag writing replaces information in every audio file, so the review gate's
    audit rule covers it — yet it produced no records at all until #86. One album
    line naming what was attempted, then one per track actually written."""
    from harmonist import activity_store
    from harmonist.activity_store import Source

    activity_store.init(tmp_path / "audit.db")
    album_dir = album_with_tracks(2)
    tagger.tag_album(album_dir, _release_2_tracks())

    rows = [e.message for e in activity_store.recent(50, source=Source.AUDIT)]
    album_line = next(m for m in rows if m.startswith("tag.album"))
    assert "release=rel-aaa" in album_line
    assert "tracks=2" in album_line
    assert "mode=full" in album_line

    tracks = [m for m in rows if m.startswith("tag.track")]
    assert len(tracks) == 2
    assert any("01 Track 1.m4a" in m for m in tracks)


def _detail():
    """Every stored tag-change record, in the order it was written.

    Goes to the table directly for the ids because `recent()` deliberately
    doesn't expose row ids — the page gets them from the join, not the feed.
    """
    from harmonist import activity_store

    conn = activity_store._ensure()
    ids = [r[0] for r in conn.execute("SELECT event_id FROM tag_changes ORDER BY event_id")]
    detail = activity_store.tag_changes_for(ids)
    return [detail[i] for i in ids]


def test_tagging_records_every_field_it_set_on_an_untagged_album(album_with_tracks, tmp_path):
    """The first tag of an untagged album records each field as absent-to-value,
    per file. That is what a revert would need to strip it all back off."""
    from harmonist import activity_store

    activity_store.init(tmp_path / "audit.db")
    album_dir = album_with_tracks(2)
    tagger.tag_album(album_dir, _release_2_tracks())

    rows = _detail()
    assert len(rows) == 2  # one per file, not one per album
    first = rows[0]
    assert first.changes["title"][0] is None  # absent before
    assert first.changes["title"][1] == "Track 1"
    assert first.changes["album"] == [None, "Test Album"]
    # Identity travels with the record so a later revert can find the file
    # again after a rename or a renumber.
    assert first.position == "1"
    assert first.track_ref is not None
    assert first.rec_ref is not None


def test_retagging_the_same_release_records_nothing(album_with_tracks, tmp_path):
    """The no-op case, and the reason the gardener (#32) won't flood history:
    a re-tag that finds MusicBrainz unchanged writes no detail at all."""
    from harmonist import activity_store

    activity_store.init(tmp_path / "audit.db")
    album_dir = album_with_tracks(2)
    release = _release_2_tracks()

    tagger.tag_album(album_dir, release)
    after_first = len(_detail())
    tagger.tag_album(album_dir, release)

    assert len(_detail()) == after_first
    # The album line still records that it ran — silence is about the per-field
    # detail, not about hiding that Harmonist touched the files.
    from harmonist.activity_store import Source

    album_lines = [
        e.message
        for e in activity_store.recent(50, source=Source.AUDIT)
        if e.message.startswith("tag.album")
    ]
    assert len(album_lines) == 2


def test_tagging_records_a_field_the_new_release_removed(album_with_tracks, tmp_path):
    """#149 made a re-tag REMOVE tags the new release lacks. This is what makes
    that removal visible: without it the audit says a track was rewritten but
    not that its label was taken away."""
    from harmonist import activity_store

    activity_store.init(tmp_path / "audit.db")
    album_dir = album_with_tracks(2)
    with_label = _release_2_tracks()
    tagger.tag_album(album_dir, with_label)
    before = len(_detail())

    tagger.tag_album(album_dir, {**with_label, "label-info-list": []})

    removals = _detail()[before:]
    assert len(removals) == 2
    assert removals[0].changes["label"][1] in (None, [])
    assert removals[0].changes["label"][0]  # there WAS a label, and we know it
    # Only what changed — the untouched fields stay out of the record.
    assert "title" not in removals[0].changes


def test_tagging_records_artwork_replacement_by_digest(album_with_tracks, tmp_path):
    """Artwork isn't an owned tag, but replacing it is destructive and belongs
    in the record. The digests are what #131 will store the images under."""
    from harmonist import activity_store
    from harmonist.formats import owned

    activity_store.init(tmp_path / "audit.db")
    album_dir = album_with_tracks(2)
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xff" + b"first" * 40)

    tagger.tag_album(album_dir, _release_2_tracks(), cover)
    first = _detail()[0].changes[owned.ARTWORK]
    assert first[0] is None  # no embedded art before
    assert len(first[1]) == 64  # sha256 hex

    newer = tmp_path / "cover2.jpg"
    newer.write_bytes(b"\xff\xd8\xff" + b"second" * 40)
    before = len(_detail())
    tagger.tag_album(album_dir, _release_2_tracks(), newer)

    replaced = _detail()[before:][0].changes[owned.ARTWORK]
    assert replaced[0] == first[1]  # what was there is what we recorded before
    assert replaced[1] != replaced[0]


def test_tagging_without_a_cover_does_not_read_the_files_artwork(
    album_with_tracks, tmp_path, monkeypatch
):
    """With no cover to embed, `write_tags` leaves existing art alone — so
    nothing about artwork can change, and reading it would be a wasted pass over
    every file on the path where scanning is already the slow part (#44, #74).

    This used to be free: the read sat behind `cover is not None and ...` and
    Python short-circuited it. Hoisting the digests out for #86 silently removed
    that guard, which is the kind of regression no assertion about OUTPUT can
    see."""
    from harmonist import formats

    reads: list[str] = []
    real = formats.read_cover

    def counting_read_cover(path):
        reads.append(path.name)
        return real(path)

    monkeypatch.setattr(formats, "read_cover", counting_read_cover)

    album_dir = album_with_tracks(2)
    tagger.tag_album(album_dir, _release_2_tracks())  # no cover_path

    assert reads == []


def _cover(tmp_path, name: str, body: bytes):
    p = tmp_path / name
    p.write_bytes(b"\xff\xd8\xff" + body)
    return p


def test_replacing_embedded_art_keeps_a_copy_of_what_it_destroyed(album_with_tracks, tmp_path):
    """#131: embedding a cover overwrites whatever the track carried, and until
    now that image was simply gone. The copy is what makes it undoable."""
    from harmonist import activity_store, artwork_store, formats

    activity_store.init(tmp_path / "audit.db")
    artwork_store.configure(tmp_path / "artwork")
    album_dir = album_with_tracks(2)

    tagger.tag_album(album_dir, _release_2_tracks(), _cover(tmp_path, "a.jpg", b"first" * 40))
    original = formats.read_cover(min(album_dir.iterdir()))
    assert original is not None
    was = artwork_store.digest(original[0])

    tagger.tag_album(album_dir, _release_2_tracks(), _cover(tmp_path, "b.jpg", b"second" * 40))

    kept = artwork_store.path_for(was)
    assert kept is not None, "the overwritten image was not kept"
    assert kept.read_bytes() == original[0]


def test_the_shared_cover_of_an_album_is_kept_once_not_once_per_track(album_with_tracks, tmp_path):
    from harmonist import activity_store, artwork_store

    activity_store.init(tmp_path / "audit.db")
    artwork_store.configure(tmp_path / "artwork")
    album_dir = album_with_tracks(2)

    tagger.tag_album(album_dir, _release_2_tracks(), _cover(tmp_path, "a.jpg", b"first" * 40))
    tagger.tag_album(album_dir, _release_2_tracks(), _cover(tmp_path, "b.jpg", b"second" * 40))

    assert len(list((tmp_path / "artwork").iterdir())) == 1


def test_re_tagging_with_the_same_cover_keeps_nothing(album_with_tracks, tmp_path):
    """Nothing is being destroyed, so nothing is backed up — and the doomed-art
    pass does no reads at all."""
    from harmonist import activity_store, artwork_store

    activity_store.init(tmp_path / "audit.db")
    artwork_store.configure(tmp_path / "artwork")
    album_dir = album_with_tracks(2)
    cover = _cover(tmp_path, "a.jpg", b"first" * 40)

    tagger.tag_album(album_dir, _release_2_tracks(), cover)
    tagger.tag_album(album_dir, _release_2_tracks(), cover)

    assert not (tmp_path / "artwork").exists() or not list((tmp_path / "artwork").iterdir())


def test_art_that_is_preserved_rather_than_replaced_is_not_backed_up(album_with_tracks, tmp_path):
    """Per-track art cancels the embed, so nothing is destroyed. Backing it up
    anyway would fill the store with images that were never at risk — which is
    why the copy happens AFTER that decision, not during the digest pass."""
    from harmonist import activity_store, artwork_store, formats
    from harmonist.formats.types import TagSet

    activity_store.init(tmp_path / "audit.db")
    artwork_store.configure(tmp_path / "artwork")
    album_dir = album_with_tracks(2)

    # Give the two tracks DIFFERENT embedded images.
    for i, path in enumerate(sorted(album_dir.iterdir())):
        formats.write_tags(
            path,
            TagSet(
                mb_album_id="m",
                album="A",
                album_artist="AA",
                title="T",
                artist="Ar",
                track_num=i + 1,
                track_total=2,
            ),
            b"\xff\xd8\xff" + f"per-track-{i}".encode() * 20,
        )

    tagger.tag_album(album_dir, _release_2_tracks(), _cover(tmp_path, "album.jpg", b"x" * 100))

    assert not (tmp_path / "artwork").exists() or not list((tmp_path / "artwork").iterdir())


def test_tag_album_audits_the_album_line_before_writing(album_with_tracks, tmp_path, monkeypatch):
    """Recorded BEFORE the loop, per the gate's "before/as it acts": a crash
    part-way must leave evidence of what was attempted, not silence."""
    from harmonist import activity_store, formats
    from harmonist.activity_store import Source

    activity_store.init(tmp_path / "audit.db")
    album_dir = album_with_tracks(2)

    def boom(*a, **kw):
        raise OSError("disk died mid-write")

    monkeypatch.setattr(formats, "write_tags", boom)
    with pytest.raises(OSError):
        tagger.tag_album(album_dir, _release_2_tracks())

    rows = [e.message for e in activity_store.recent(50, source=Source.AUDIT)]
    assert any(m.startswith("tag.album") for m in rows), "no evidence of the attempt"
    assert not [m for m in rows if m.startswith("tag.track")]  # nothing was written


def test_tag_album_writes_all_album_atoms(album_with_tracks):
    album_dir = album_with_tracks(2)
    tagger.tag_album(album_dir, _release_2_tracks())

    track1 = MP4(album_dir / "01 Track 1.m4a")
    assert _atom_str(track1, ATOM_MB_ALBUM_ID) == "rel-aaa"
    assert _atom_strs(track1, ATOM_MB_ALBUM_ARTIST_ID) == ["art-aaa"]
    assert _atom_str(track1, ATOM_MB_RELEASE_GROUP_ID) == "rg-aaa"
    # Both lower-cased, matching Picard — see #290.
    assert _atom_str(track1, ATOM_MB_ALBUM_TYPE) == "album"
    assert _atom_str(track1, ATOM_MB_ALBUM_STATUS) == "official"
    assert _atom_str(track1, ATOM_MB_ALBUM_COUNTRY) == "GB"
    assert _atom_str(track1, ATOM_LABEL) == "Test Label"
    assert _atom_str(track1, ATOM_CATALOG) == "CAT-001"
    assert _atom_str(track1, ATOM_BARCODE) == "0123456789012"
    assert _atom_str(track1, ATOM_ASIN) == "B00ASIN1234"
    assert _atom_str(track1, ATOM_MEDIA) == "Digital Media"


def test_tag_album_writes_per_track_atoms(album_with_tracks):
    album_dir = album_with_tracks(2)
    tagger.tag_album(album_dir, _release_2_tracks())

    track1 = MP4(album_dir / "01 Track 1.m4a")
    track2 = MP4(album_dir / "02 Track 2.m4a")

    assert _atom_str(track1, ATOM_MB_TRACK_ID) == "rec-001"
    assert _atom_str(track1, ATOM_MB_RELEASE_TRACK_ID) == "rt-001"
    assert _atom_strs(track1, ATOM_ISRC) == ["GBTEST2100001"]
    assert _atom_str(track2, ATOM_MB_TRACK_ID) == "rec-002"
    assert _atom_str(track2, ATOM_MB_RELEASE_TRACK_ID) == "rt-002"
    # Track 2's recording has no ISRC → the atom is omitted.
    assert ATOM_ISRC not in track2


def test_tag_album_writes_standard_text_tags(album_with_tracks):
    album_dir = album_with_tracks(2)
    tagger.tag_album(album_dir, _release_2_tracks())

    track1 = MP4(album_dir / "01 Track 1.m4a")
    assert track1[ATOM_TITLE] == ["Track 1"]
    assert track1[ATOM_ALBUM] == ["Test Album"]
    assert track1[ATOM_ARTIST] == ["Test Artist"]
    assert track1[ATOM_ALBUM_ARTIST] == ["Test Artist"]
    assert track1[ATOM_DATE] == ["2021-06-15"]
    assert track1[ATOM_TRACK_NUM] == [(1, 2)]
    assert track1[ATOM_DISC_NUM] == [(1, 1)]


def test_tag_album_writes_sort_artists_original_date_script(album_with_tracks):
    album_dir = album_with_tracks(2)
    tagger.tag_album(album_dir, _release_2_tracks())

    track1 = MP4(album_dir / "01 Track 1.m4a")
    # Native sort atoms (Picard: soaa / soar)
    assert track1[ATOM_ALBUM_ARTIST_SORT] == ["Test Artist, The"]
    assert track1[ATOM_ARTIST_SORT] == ["Test Artist, The"]
    # Multi-value artists (freeform, no join phrases)
    assert _atom_strs(track1, ATOM_ARTISTS) == ["Test Artist"]
    # Original (release-group) date + derived year
    assert _atom_str(track1, ATOM_ORIGINAL_DATE) == "2019-01-01"
    assert _atom_str(track1, ATOM_ORIGINAL_YEAR) == "2019"
    # Script from text-representation
    assert _atom_str(track1, ATOM_SCRIPT) == "Latn"

    # Track 2's own artist-credit drives its sort + artists tags.
    track2 = MP4(album_dir / "02 Track 2.m4a")
    assert track2[ATOM_ARTIST_SORT] == ["Featured Artist feat. Other, The"]
    assert _atom_strs(track2, ATOM_ARTISTS) == ["Featured Artist", "Other"]


@pytest.mark.parametrize(
    "credit",
    [
        # The shape musicbrainzngs actually returns: join phrases are bare strings.
        pytest.param(
            [
                {"artist": {"name": "zakè", "sort-name": "zakè"}, "name": "zakè"},
                " & ",
                {"artist": {"name": "rhubiqs", "sort-name": "rhubiqs"}, "name": "rhubiqs"},
            ],
            id="sibling-string",
        ),
        # The JSON web service's shape, tolerated in case the client is swapped (#26).
        pytest.param(
            [
                {
                    "artist": {"name": "zakè", "sort-name": "zakè"},
                    "name": "zakè",
                    "joinphrase": " & ",
                },
                {"artist": {"name": "rhubiqs", "sort-name": "rhubiqs"}, "name": "rhubiqs"},
            ],
            id="joinphrase-key",
        ),
    ],
)
def test_artist_phrases_keep_the_join_phrase(credit):
    """#183: the sort phrase dropped the join phrase entirely, because it only read
    `joinphrase` off the dict — the shape musicbrainzngs never produces. Both walkers
    must agree, whichever shape the credit arrives in."""
    assert tagger._artist_phrase(credit) == "zakè & rhubiqs"
    assert tagger._artist_sort_phrase(credit) == "zakè & rhubiqs"
    assert tagger._artist_names(credit) == ["zakè", "rhubiqs"]


def test_a_collaboration_names_both_album_artists_on_every_track():
    """#322. `album_artist` is one joined phrase, so a player filing the album
    under BOTH artists has to guess where "zakè" ends and "rhubiqs" begins —
    the guess `artists` already removes at track level.

    Asserted through `tagsets_for` rather than on a helper, because the claim is
    that EVERY track carries the album-level list: it is derived from the release
    credit, so it cannot vary between tracks, and a per-track derivation would
    still pass a one-track assertion.
    """
    release = _release_2_tracks()
    release["artist-credit"] = [
        {"artist": {"id": "art-z", "name": "zakè", "sort-name": "zakè"}, "name": "zakè"},
        " & ",
        {"artist": {"id": "art-r", "name": "rhubiqs", "sort-name": "rhubiqs"}, "name": "rhubiqs"},
    ]

    tagsets = tagger.tagsets_for(release)

    assert [t.album_artists for t in tagsets] == [["zakè", "rhubiqs"], ["zakè", "rhubiqs"]]
    # The joined phrase is unchanged — the list is an addition, not a substitute.
    assert {t.album_artist for t in tagsets} == {"zakè & rhubiqs"}
    # Track 2 keeps its own credit in `artists`, which is what makes the two
    # fields different questions rather than one written twice.
    assert tagsets[1].artists == ["Featured Artist", "Other"]


def _various_artists_release() -> dict:
    """`_release_2_tracks` credited to MusicBrainz's Various Artists, with each
    track credited to its own artist — the shape of a real compilation."""
    release = _release_2_tracks()
    release["artist-credit"] = [
        {
            "artist": {
                "id": tagger.VARIOUS_ARTISTS_ID,
                "name": "Various Artists",
                "sort-name": "Various Artists",
            },
            "name": "Various Artists",
        },
    ]
    tracks = release["medium-list"][0]["track-list"]
    tracks[0]["artist-credit"] = [
        {"artist": {"id": "art-ccc", "name": "Kangding Ray", "sort-name": "Kangding Ray"}},
    ]
    return release


def test_a_various_artists_release_carries_the_compilation_flag():
    """#323. Without it Plex and every iTunes-lineage player shatter a VA
    compilation into one album per track artist.

    Album-scoped, so the flag is on EVERY track however the tracks are credited
    — which is the whole point: a player groups by what the files agree on.
    """
    tagsets = tagger.tagsets_for(_various_artists_release())

    assert [t.compilation for t in tagsets] == [True, True]


def test_an_ordinary_release_carries_no_compilation_flag():
    """Absent, not False. Harmonist writes the tag only when set, so an album
    that stops being a compilation has it removed by the owned-set clear (#149)
    with no extra code — and `owned.as_flag` never produces a False that would
    read as a value and differ from MusicBrainz forever."""
    assert [t.compilation for t in tagger.tagsets_for(_release_2_tracks())] == [None, None]


def test_every_label_and_catalogue_number_is_written(tmp_path):
    """#334. Picard collects EVERY `label-info` entry into two lists, and the
    two independently of each other (`picard/mbjson.py:756`,
    `label_info_from_node`). Co-releases and licensed reissues carry two labels
    routinely, and Harmonist kept only the first.
    """
    release = _release_2_tracks()
    release["label-info-list"] = [
        {"label": {"name": "Kompakt"}, "catalog-number": "KOM 001"},
        {"label": {"name": "Studio !K7"}, "catalog-number": "K7 999"},
    ]

    tags = tagger.tagsets_for(release)[0]

    assert tags.label == ["Kompakt", "Studio !K7"]
    assert tags.catalog_number == ["KOM 001", "K7 999"]


def test_a_catalogue_number_on_a_later_label_is_not_lost(tmp_path):
    """The half of #334 that fires on Harmonist's OWN output, not just adopted
    files.

    Both fields came off `label-info[0]`, so a release whose first entry names a
    label but carries no catalogue number — while a later entry has one — got no
    catalogue number written at all. Picard writes it, because it walks the two
    fields independently.
    """
    release = _release_2_tracks()
    release["label-info-list"] = [
        {"label": {"name": "Kompakt"}},
        {"label": {"name": "Kompakt"}, "catalog-number": "KOM 001"},
    ]

    tags = tagger.tagsets_for(release)[0]

    assert tags.catalog_number == ["KOM 001"]
    # Deduped, like Picard's — the same label named twice is one label.
    assert tags.label == ["Kompakt"]


def test_a_greatest_hits_release_by_one_artist_is_not_a_compilation():
    """The wrong turn this rule is most likely to take.

    MusicBrainz's release-group `compilation` SECONDARY TYPE describes the
    release group's nature, so a one-artist greatest-hits album carries it — and
    flagging one of those is precisely what makes a player split the album apart.
    The flag is about the release ARTIST, and nothing else.
    """
    release = _release_2_tracks()
    release["release-group"]["secondary-type-list"] = ["Compilation"]

    assert tagger.tagsets_for(release)[0].compilation is None


def test_a_band_merely_named_various_artists_is_not_flagged():
    """The identity is an id comparison, never a name match (review-gate item 2).

    Matching the string would guess at an identity MusicBrainz gives exactly —
    and would be wrong in both directions: it flags a real act with that name,
    and it misses a release credited to Various Artists in another language.
    """
    release = _release_2_tracks()
    release["artist-credit"] = [
        {
            "artist": {"id": "art-imposter", "name": "Various Artists", "sort-name": "V"},
            "name": "Various Artists",
        },
    ]

    assert tagger.tagsets_for(release)[0].compilation is None


def test_tag_album_track_artist_credit_overrides_release(album_with_tracks):
    """Track 2 has its own artist-credit; should be used for ©ART and Artist Id atom."""
    album_dir = album_with_tracks(2)
    tagger.tag_album(album_dir, _release_2_tracks())

    track2 = MP4(album_dir / "02 Track 2.m4a")
    assert track2[ATOM_ARTIST] == ["Featured Artist feat. Other"]
    assert _atom_strs(track2, ATOM_MB_ARTIST_ID) == ["art-bbb", "art-ccc"]
    # Album-artist remains the release's primary
    assert track2[ATOM_ALBUM_ARTIST] == ["Test Artist"]


def test_tag_album_prefers_track_title_over_recording_title(album_with_tracks):
    """Regression for #27: the per-release track title wins over the recording
    title. After an editor applies MB featured-artist style, the guest moves out
    of the *track* title into the artist credit while the *recording* title keeps
    its original form — re-tagging must write the track title, not the stale
    recording title."""
    album_dir = album_with_tracks(2)
    release = _release_2_tracks()
    # Track 1: release track title differs from its recording title.
    release["medium-list"][0]["track-list"][0]["title"] = "Ground Glass"
    release["medium-list"][0]["track-list"][0]["recording"]["title"] = (
        "Ground Glass /w Foxes in Fiction"
    )
    tagger.tag_album(album_dir, release)

    track1 = MP4(album_dir / "01 Track 1.m4a")
    assert track1[ATOM_TITLE] == ["Ground Glass"]


def test_tag_album_falls_back_to_recording_title(album_with_tracks):
    """When a track carries no per-release title of its own, fall back to the
    recording title rather than writing an empty title."""
    album_dir = album_with_tracks(2)
    release = _release_2_tracks()
    del release["medium-list"][0]["track-list"][0]["title"]
    release["medium-list"][0]["track-list"][0]["recording"]["title"] = "Recording Only"
    tagger.tag_album(album_dir, release)

    track1 = MP4(album_dir / "01 Track 1.m4a")
    assert track1[ATOM_TITLE] == ["Recording Only"]


def test_tag_album_removes_legacy_release_id(album_with_tracks):
    album_dir = album_with_tracks(1)
    f = album_dir / "01 Track 1.m4a"
    audio = MP4(f)
    audio[LEGACY_RELEASE_ID] = [b"old-broken-mbid"]
    audio.save()

    tagger.tag_album(album_dir, _single_track_release())
    audio2 = MP4(f)
    assert LEGACY_RELEASE_ID not in audio2


def test_tag_album_preserves_comment_atom(album_with_tracks):
    album_dir = album_with_tracks(1)
    f = album_dir / "01 Track 1.m4a"
    audio = MP4(f)
    audio[ATOM_COMMENT] = ["https://myartist.bandcamp.com/album/y"]
    audio.save()

    tagger.tag_album(album_dir, _single_track_release())
    audio2 = MP4(f)
    assert audio2[ATOM_COMMENT] == ["https://myartist.bandcamp.com/album/y"]


def test_tag_album_idempotent(album_with_tracks):
    """Running twice produces the same atom values — no duplication, no error."""
    album_dir = album_with_tracks(1)
    rel = _single_track_release()

    tagger.tag_album(album_dir, rel)
    audio_first = dict(MP4(album_dir / "01 Track 1.m4a"))
    tagger.tag_album(album_dir, rel)
    audio_second = dict(MP4(album_dir / "01 Track 1.m4a"))

    assert audio_first == audio_second


def test_tag_album_count_mismatch_raises(album_with_tracks):
    album_dir = album_with_tracks(3)  # 3 files
    with pytest.raises(TagMismatchError):
        tagger.tag_album(album_dir, _single_track_release())


def test_tag_album_embeds_cover_art(album_with_tracks, tmp_path):
    album_dir = album_with_tracks(1)
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(_minimal_jpeg())

    tagger.tag_album(album_dir, _single_track_release(), cover_path=cover)
    audio = MP4(album_dir / "01 Track 1.m4a")
    assert ATOM_COVER in audio
    assert len(audio[ATOM_COVER]) == 1
    assert bytes(audio[ATOM_COVER][0]) == _minimal_jpeg()


def test_tag_album_no_cover_when_path_none(album_with_tracks):
    album_dir = album_with_tracks(1)
    tagger.tag_album(album_dir, _single_track_release(), cover_path=None)
    audio = MP4(album_dir / "01 Track 1.m4a")
    assert ATOM_COVER not in audio


def _embed_cover(path, data: bytes) -> None:
    audio = MP4(path)
    audio[ATOM_COVER] = [MP4Cover(data, imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()


def test_tag_album_preserves_per_track_artwork(album_with_tracks, tmp_path):
    """DATA SAFETY: tracks with DIFFERENT embedded covers (per-track art, e.g. a
    compilation) are preserved — the album cover is NOT embedded over them."""
    album_dir = album_with_tracks(2)
    art_a = _minimal_jpeg()
    art_b = _minimal_jpeg() + b"_different"
    _embed_cover(album_dir / "01 Track 1.m4a", art_a)
    _embed_cover(album_dir / "02 Track 2.m4a", art_b)
    new_cover = tmp_path / "cover.jpg"
    new_cover.write_bytes(b"\xff\xd8\xff\xe0NEW_ALBUM_COVER\xff\xd9")

    tagger.tag_album(album_dir, _release_2_tracks(), cover_path=new_cover)

    assert bytes(MP4(album_dir / "01 Track 1.m4a")[ATOM_COVER][0]) == art_a  # preserved
    assert bytes(MP4(album_dir / "02 Track 2.m4a")[ATOM_COVER][0]) == art_b  # preserved


def test_preserved_per_track_artwork_reaches_the_album_history(album_with_tracks, tmp_path):
    """#260: declining to overwrite the user's artwork is a decision Harmonist
    made about their files, so it belongs in that album's own History — not only
    in the global feed as an unattributed mirrored warning.

    The `art=preserved` token on the `tag.album` audit line is not this: it shows
    only with "Show details" ticked, as one token among a dozen. This is the
    sentence that says it.
    """
    from datetime import UTC, datetime

    from harmonist import activity, activity_store
    from harmonist import sidecar as sidecar_mod
    from harmonist.models import Sidecar

    activity_store.init(tmp_path / "activity.db")
    activity.install_log_handler()
    album_dir = album_with_tracks(2)
    sidecar_mod.write(
        album_dir, Sidecar(mb_release_id="rel-aaa", tagged_at=datetime(2026, 1, 1, tzinfo=UTC))
    )
    _embed_cover(album_dir / "01 Track 1.m4a", _minimal_jpeg())
    _embed_cover(album_dir / "02 Track 2.m4a", _minimal_jpeg() + b"_different")
    new_cover = tmp_path / "cover.jpg"
    new_cover.write_bytes(b"\xff\xd8\xff\xe0NEW_ALBUM_COVER\xff\xd9")

    tagger.tag_album(album_dir, _release_2_tracks(), cover_path=new_cover)

    entry = next(
        e
        for e in activity_store.album_history("rel-aaa")
        if "per-track embedded artwork" in e.message
    )
    assert entry.level == "warning"
    assert entry.album_label == "Test Artist — Test Album"


def test_tag_album_overwrite_art_forces_replacement(album_with_tracks, tmp_path):
    """overwrite_art=True is the explicit override: embed the album cover even over
    differing per-track art."""
    album_dir = album_with_tracks(2)
    _embed_cover(album_dir / "01 Track 1.m4a", _minimal_jpeg())
    _embed_cover(album_dir / "02 Track 2.m4a", _minimal_jpeg() + b"_different")
    new = b"\xff\xd8\xff\xe0FORCED\xff\xd9"
    new_cover = tmp_path / "cover.jpg"
    new_cover.write_bytes(new)

    tagger.tag_album(album_dir, _release_2_tracks(), cover_path=new_cover, overwrite_art=True)

    assert bytes(MP4(album_dir / "01 Track 1.m4a")[ATOM_COVER][0]) == new
    assert bytes(MP4(album_dir / "02 Track 2.m4a")[ATOM_COVER][0]) == new


def test_tag_album_embeds_when_art_missing_on_some_tracks(album_with_tracks, tmp_path):
    """'Missing on some tracks' is NOT per-track art — only ACTUAL differences are.
    One track with a cover + one without → the album cover embeds normally."""
    album_dir = album_with_tracks(2)
    _embed_cover(album_dir / "01 Track 1.m4a", _minimal_jpeg())  # track 2 has none
    new = b"\xff\xd8\xff\xe0UNIFORM\xff\xd9"
    new_cover = tmp_path / "cover.jpg"
    new_cover.write_bytes(new)

    tagger.tag_album(album_dir, _release_2_tracks(), cover_path=new_cover)

    assert bytes(MP4(album_dir / "01 Track 1.m4a")[ATOM_COVER][0]) == new
    assert bytes(MP4(album_dir / "02 Track 2.m4a")[ATOM_COVER][0]) == new


def test_tag_album_returns_count(album_with_tracks):
    album_dir = album_with_tracks(2)
    n = tagger.tag_album(album_dir, _release_2_tracks())
    assert n == 2


# ---------- incomplete-mode tagging (§15.3) ----------


def test_tag_album_incomplete_allows_fewer_files_than_tracks(album_with_tracks):
    """incomplete=True bypasses the count-mismatch raise."""
    album_dir = album_with_tracks(1)  # 1 file
    n = tagger.tag_album(album_dir, _release_2_tracks(), incomplete=True)
    assert n == 1
    audio = MP4(album_dir / "01 Track 1.m4a")
    # The single on-disk file should be tagged with the album MBID
    assert _atom_str(audio, ATOM_MB_ALBUM_ID) == "rel-aaa"


def test_tag_album_incomplete_still_raises_when_too_many_files(album_with_tracks):
    """file_count > track_count is out of scope (per §15.3) — still raises
    even in incomplete mode.
    """
    album_dir = album_with_tracks(3)  # 3 files, release has 1 track
    with pytest.raises(TagMismatchError, match="exceeds"):
        tagger.tag_album(album_dir, _single_track_release(), incomplete=True)


def test_tag_album_incomplete_uses_positional_fallback_without_lengths(
    album_with_tracks,
):
    """With no track lengths in the MB release, incomplete-mode assigns
    files positionally — file 0 → MB track 0.
    """
    album_dir = album_with_tracks(1)  # one file
    tagger.tag_album(album_dir, _release_2_tracks(), incomplete=True)
    audio = MP4(album_dir / "01 Track 1.m4a")
    # MB track 0 ("Track 1") should be the assignment
    assert _atom_str(audio, ATOM_MB_TRACK_ID) == "rec-001"


def test_tag_album_incomplete_ignores_length_similarity(album_with_tracks):
    """Length is not an identity (#232).

    The sine.m4a fixture is ~1000ms. Track 1 is 10s and track 2 is 1s, so the
    old rule assigned this file to track 2 on the strength of its duration —
    against a file that says nothing about which track it is, where file order
    is the only thing on offer that isn't invention. Two tracks of one length
    are ordinary on a real release, and being wrong here writes another track's
    title and ids into the file.
    """
    album_dir = album_with_tracks(1)
    release = _release_2_tracks()
    release["medium-list"][0]["track-list"][0]["recording"]["length"] = "10000"
    release["medium-list"][0]["track-list"][1]["recording"]["length"] = "1000"

    tagger.tag_album(album_dir, release, incomplete=True)

    audio = MP4(album_dir / "01 Track 1.m4a")
    assert _atom_str(audio, ATOM_MB_TRACK_ID) == "rec-001"


def test_tag_album_incomplete_is_idempotent(album_with_tracks):
    """Twice is the same as once, across the rung change that the first run
    causes: the file starts with no id and is placed by file order, the tagging
    writes that slot's id into it, and the second run then reads the id and
    reaches the same slot by a different rung.

    (Which also means a placement the user disagrees with is now written down
    rather than re-derived. #136 is where overruling it belongs — it does not
    become harder to correct, but it does become explicit.)
    """
    album_dir = album_with_tracks(1)
    release = _release_2_tracks()

    tagger.tag_album(album_dir, release, incomplete=True)
    first = dict(MP4(album_dir / "01 Track 1.m4a"))
    tagger.tag_album(album_dir, release, incomplete=True)

    assert dict(MP4(album_dir / "01 Track 1.m4a")) == first


def test_tag_album_incomplete_assigns_by_release_track_id(album_with_tracks):
    """A file that already names its slot is tagged as that slot, wherever it
    sits in the folder — the rung that isn't a guess.

    Positional would call this file track 1; it says it is track 2, in the only
    terms that can't be argued with, and it is the only file present.
    """
    album_dir = album_with_tracks(1)
    f = album_dir / "01 Track 1.m4a"
    audio = MP4(f)
    audio[ATOM_MB_RELEASE_TRACK_ID] = [b"rt-002"]
    audio.save()

    tagger.tag_album(album_dir, _release_2_tracks(), incomplete=True)

    tagged = MP4(f)
    assert _atom_str(tagged, ATOM_MB_TRACK_ID) == "rec-002"
    assert tagged[ATOM_TITLE][0] == "Track 2"


def test_tag_album_incomplete_prefers_the_id_over_the_number(album_with_tracks):
    """The TISM case, in miniature: the file's disc/track numbers are stale
    because MusicBrainz renumbered the release, and its id is not. The id wins,
    so the numbers get corrected instead of deciding where the file goes."""
    album_dir = album_with_tracks(1)
    f = album_dir / "01 Track 1.m4a"
    audio = MP4(f)
    audio[ATOM_MB_RELEASE_TRACK_ID] = [b"rt-002"]
    audio["trkn"] = [(1, 2)]  # stale: it says track 1
    audio.save()

    tagger.tag_album(album_dir, _release_2_tracks(), incomplete=True)

    tagged = MP4(f)
    assert _atom_str(tagged, ATOM_MB_TRACK_ID) == "rec-002"
    assert tagged["trkn"][0][0] == 2, "and the stale number is rewritten"


# -- helpers used in multiple tests --


def _single_track_release() -> dict:
    return {
        "id": "rel-aaa",
        "title": "Test Album",
        "status": "Official",
        "artist-credit": [
            {"artist": {"id": "art-aaa", "name": "Test Artist"}, "name": "Test Artist"},
        ],
        "release-group": {"id": "rg-aaa", "primary-type": "Album"},
        "medium-list": [
            {
                "position": "1",
                "track-list": [
                    {
                        "id": "rt-001",
                        "position": "1",
                        "title": "Track 1",
                        "recording": {"id": "rec-001", "title": "Track 1"},
                    },
                ],
            }
        ],
    }


def _minimal_jpeg() -> bytes:
    """Arbitrary bytes with a JPEG magic header — mutagen doesn't validate."""
    return b"\xff\xd8\xff\xe0" + b"FAKE_JPEG_BODY" * 8 + b"\xff\xd9"


# ---------- undoing a tagging (#157) ----------


def _plan(last: int | None = None):
    """The revert plan for the tagging that just ran, built the way the route
    builds it — from the stored records, through the shared grouping.

    `last` takes only the final N records, which is how a test with more than one
    tagging names the most recent one. `_detail()` returns every stored record,
    and `activity_store.clear()` does not remove them (#165), so slicing is the
    honest way to say "the tagging that just ran" here. The route doesn't need
    this — it groups by action id.
    """
    from harmonist import tag_history

    records = _detail()
    return tag_history.revert_plan(records[-last:] if last else records)


def test_reverting_a_first_tagging_strips_the_tags_back_off(album_with_tracks, tmp_path):
    """The commonest undo: an album tagged for the first time, put back to how
    it arrived. Every field was absent before, so reverting means REMOVING the
    tags — which is exactly what a TagSet can't express and `write_owned` can."""
    from harmonist import activity_store, formats

    activity_store.init(tmp_path / "audit.db")
    album_dir = album_with_tracks(2)
    tagger.tag_album(album_dir, _release_2_tracks())
    tagged = formats.read_owned(next(album_dir.glob("*.m4a")))
    assert tagged["album"] == "Test Album"

    outcome = tagger.revert_tags(album_dir, _plan())

    assert outcome.files == 2
    assert "album" in outcome.restored
    after = formats.read_owned(next(album_dir.glob("*.m4a")))
    assert after["album"] is None
    assert after["title"] is None
    assert after["mb_album_id"] is None
    assert after["isrcs"] == []


def test_reverting_leaves_a_field_that_changed_since_alone(album_with_tracks, tmp_path):
    """The staleness guard. An undo names one tagging; reaching past it into a
    later change — or into an edit the user made in Picard — would undo
    something the button never claimed to."""
    from harmonist import activity_store, formats

    activity_store.init(tmp_path / "audit.db")
    album_dir = album_with_tracks(2)
    tagger.tag_album(album_dir, _release_2_tracks())
    plan = _plan()

    # Someone edits the album title afterwards — Picard, or another tool. Both
    # files, so the field is uniformly stale and the outcome has one answer.
    for f in album_dir.glob("*.m4a"):
        formats.write_owned(f, {**formats.read_owned(f), "album": "Edited By Hand"})
    f = next(album_dir.glob("*.m4a"))

    outcome = tagger.revert_tags(album_dir, plan)

    assert "album" in outcome.stale
    assert "album" not in outcome.restored
    after = formats.read_owned(f)
    assert after["album"] == "Edited By Hand", "the later edit survived"
    assert after["title"] is None, "everything else still went back"


def test_reverting_reports_the_release_id_it_removed(album_with_tracks, tmp_path):
    """The caller has to keep the sidecar in step (#158), so the outcome says
    both that identity moved and what the files carry now. None is a real
    answer — the id was removed — so the flag can't be folded into the value."""
    from harmonist import activity_store, formats

    activity_store.init(tmp_path / "audit.db")
    album_dir = album_with_tracks(2)
    tagger.tag_album(album_dir, _release_2_tracks())

    outcome = tagger.revert_tags(album_dir, _plan())

    assert outcome.release_id_reverted is True
    assert outcome.release_id_now is None, "a first tagging had no id before it"
    assert formats.read_owned(next(album_dir.glob("*.m4a")))["mb_album_id"] is None


def test_reverting_a_rematch_puts_back_the_older_release_id(album_with_tracks, tmp_path):
    """Undoing a re-match reverts identity to the release the album had BEFORE
    it — not to nothing. That older release is what the sidecar must follow, and
    what the user asked to go back to."""
    from harmonist import activity_store, formats

    activity_store.init(tmp_path / "audit.db")
    album_dir = album_with_tracks(2)
    tagger.tag_album(album_dir, _release_2_tracks())

    other = _release_2_tracks()
    other["id"] = "rel-bbb"
    tagger.tag_album(album_dir, other)
    assert formats.read_owned(next(album_dir.glob("*.m4a")))["mb_album_id"] == "rel-bbb"

    # The last two records are the re-match's — one per file. Undoing the FIRST
    # tagging is a different question, and this test asks about the re-match.
    outcome = tagger.revert_tags(album_dir, _plan(last=2))

    assert outcome.release_id_reverted is True
    assert outcome.release_id_now == "rel-aaa"
    assert formats.read_owned(next(album_dir.glob("*.m4a")))["mb_album_id"] == "rel-aaa"


def test_reverting_leaves_identity_alone_when_the_files_disagree(album_with_tracks, tmp_path):
    """Identity is all-or-nothing. If one file has been re-tagged since, moving
    the others would split the album between two releases — INCONSISTENT, which
    the user then has to repair by hand — and would leave the caller no single
    id to write to the sidecar."""
    from harmonist import activity_store, formats

    activity_store.init(tmp_path / "audit.db")
    album_dir = album_with_tracks(2)
    tagger.tag_album(album_dir, _release_2_tracks())
    plan = _plan()

    odd = min(album_dir.glob("*.m4a"))
    formats.write_owned(odd, {**formats.read_owned(odd), "mb_album_id": "rel-elsewhere"})

    outcome = tagger.revert_tags(album_dir, plan)

    assert outcome.release_id_reverted is False
    assert "mb_album_id" in outcome.stale
    assert formats.read_owned(odd)["mb_album_id"] == "rel-elsewhere"
    other = sorted(album_dir.glob("*.m4a"))[1]
    assert formats.read_owned(other)["mb_album_id"] == "rel-aaa", "not split in two"
    assert formats.read_owned(other)["album"] is None, "the rest still went back"


def test_reverting_twice_is_a_no_op(album_with_tracks, tmp_path):
    """Idempotent, like every other transition here — and it must be, because
    the second click of a double-click would otherwise report a second undo."""
    from harmonist import activity_store, formats

    activity_store.init(tmp_path / "audit.db")
    album_dir = album_with_tracks(2)
    tagger.tag_album(album_dir, _release_2_tracks())
    plan = _plan()

    first = tagger.revert_tags(album_dir, plan)
    snapshot = formats.read_owned(next(album_dir.glob("*.m4a")))
    second = tagger.revert_tags(album_dir, plan)

    assert first.files == 2
    assert second.files == 0, "nothing left to put back"
    assert formats.read_owned(next(album_dir.glob("*.m4a"))) == snapshot


def test_reverting_refuses_before_writing_when_a_file_is_gone(album_with_tracks, tmp_path):
    """Resolve everything before writing anything, like the artwork restore: a
    half-reverted album is a state that was never real, with neither half
    undoable."""
    from harmonist import activity_store, formats

    activity_store.init(tmp_path / "audit.db")
    album_dir = album_with_tracks(2)
    tagger.tag_album(album_dir, _release_2_tracks())
    plan = _plan()

    survivor = album_dir / "01 Track 1.m4a"
    (album_dir / "02 Track 2.m4a").unlink()
    before = formats.read_owned(survivor)

    with pytest.raises(tagger.RevertUnavailableError):
        tagger.revert_tags(album_dir, plan)

    assert formats.read_owned(survivor) == before, "the surviving file was not touched"


def test_reverting_records_what_it_undid(album_with_tracks, tmp_path):
    """An undo is a destructive write like any other, so it lands in the audit
    log with its own per-field detail — which is also what makes it undoable in
    turn, the same guarantee `restore_artwork` gives."""
    from harmonist import activity_store
    from harmonist.activity_store import Source

    activity_store.init(tmp_path / "audit.db")
    album_dir = album_with_tracks(2)
    tagger.tag_album(album_dir, _release_2_tracks())
    before_rows = len(_detail())

    tagger.revert_tags(album_dir, _plan())

    lines = [e.message for e in activity_store.recent(50, source=Source.AUDIT)]
    per_file = [m for m in lines if m.startswith("tag.revert.track")]
    assert len(per_file) == 2, "one per file, like the tagging it undoes"
    assert any("01 Track 1.m4a" in m for m in per_file)

    # And one album-level line, written BEFORE the writes, so a crash part-way
    # leaves evidence of what was attempted — the same shape as `tag.album`.
    album_line = next(m for m in lines if m.startswith("tag.revert "))
    assert "files=2" in album_line

    rows = _detail()
    assert len(rows) == before_rows + 2
    # The undo's own record is the mirror image of the tagging's.
    assert rows[-1].changes["album"] == ["Test Album", None]


def test_reverting_does_not_touch_the_artwork(album_with_tracks, tmp_path):
    """Artwork has its own undo and its own store (#131). One button per store,
    each honest about what it can do — and a tag revert that silently changed
    the cover would do more than it says."""
    from harmonist import activity_store, formats

    activity_store.init(tmp_path / "audit.db")
    album_dir = album_with_tracks(2)
    cover = _cover(tmp_path, "cover.jpg", _minimal_jpeg())
    tagger.tag_album(album_dir, _release_2_tracks(), cover)
    f = next(album_dir.glob("*.m4a"))
    embedded = formats.read_cover(f)
    assert embedded is not None

    tagger.revert_tags(album_dir, _plan())

    assert formats.read_cover(f) == embedded


# ---------- a bonus DVD is not missing audio (§15.3, #235) ----------


def _release_cd_plus_dvd() -> dict:
    """A 2-track CD and a 3-track DVD — *TISM — The White Albun* in miniature."""
    release = _release_2_tracks()
    release["medium-list"].append(
        {
            "position": "2",
            "format": "DVD-Video",
            "track-list": [
                {
                    "id": f"rt-v{i}",
                    "position": str(i),
                    "title": f"Video {i}",
                    "recording": {
                        "id": f"rec-v{i}",
                        "title": f"Video {i}",
                        "length": "300000",
                        # What MusicBrainz actually returns, and the only honest
                        # test of a video medium — the medium's `format` is not
                        # one, since a Blu-ray can carry 45 audio tracks and 4
                        # videos (#206).
                        "video": "true",
                    },
                }
                for i in range(1, 4)
            ],
        }
    )
    return release


def _with_video_media(album_dir, media):
    from datetime import UTC, datetime

    from harmonist import sidecar as sidecar_mod
    from harmonist.models import Sidecar

    sidecar_mod.write(
        album_dir,
        Sidecar(
            mb_release_id="rel-aaa",
            tagged_at=datetime(2026, 1, 1, tzinfo=UTC),
            video_media=media,
        ),
    )


def test_tag_album_does_not_count_video_tracks_against_the_files(album_with_tracks):
    """The whole CD is on disk and the bonus DVD never will be, which is what
    COMPLETE means for this album (#206) — so Re-tag has to work. It used to
    refuse: "2 audio files but MB release has 5 tracks"."""
    album_dir = album_with_tracks(2)
    _with_video_media(album_dir, (2,))

    n = tagger.tag_album(album_dir, _release_cd_plus_dvd())

    assert n == 2
    assert _atom_str(MP4(album_dir / "01 Track 1.m4a"), ATOM_MB_RELEASE_TRACK_ID) == "rt-001"
    assert _atom_str(MP4(album_dir / "02 Track 2.m4a"), ATOM_MB_RELEASE_TRACK_ID) == "rt-002"


def test_tag_album_puts_the_files_on_the_audio_medium(album_with_tracks):
    """Not merely "doesn't raise": the two files must land on the CD's tracks.
    Pairing positionally across the flattened release would have been just as
    quiet and just as wrong."""
    album_dir = album_with_tracks(2)
    _with_video_media(album_dir, (2,))

    tagger.tag_album(album_dir, _release_cd_plus_dvd())

    for name in ("01 Track 1.m4a", "02 Track 2.m4a"):
        assert MP4(album_dir / name)["disk"][0][0] == 1


def test_tag_album_still_refuses_when_audio_is_genuinely_missing(album_with_tracks):
    """The control. Forgiving the DVD must not forgive a CD track that isn't
    there — that is the mismatch the guard exists for."""
    album_dir = album_with_tracks(1)  # the CD has two tracks
    _with_video_media(album_dir, (2,))

    with pytest.raises(TagMismatchError, match="1 audio files but MB release has 2 tracks"):
        tagger.tag_album(album_dir, _release_cd_plus_dvd())


@pytest.mark.parametrize("recorded", [None, ()])
def test_tag_album_asks_the_release_not_the_sidecar_about_video(album_with_tracks, recorded):
    """#237: the sidecar's `video_media` doesn't get a vote here.

    It is written only for albums that already LOOK like they are missing a
    medium (`reconcile.needs_video_media`), which an album whose tags predate
    the release gaining discs does not — TISM's *The White Albun* restored from
    backup says "disc 1 of 1, all present", so nothing was absent, nothing was
    asked, and the guard went back to counting videos as missing audio.

    `None` is that album ("not asked"), and `()` is the staler version of the
    same thing ("asked, before the DVDs existed"). The release in hand carries
    the per-track video flag either way, so neither blocks the re-tag.
    """
    album_dir = album_with_tracks(2)
    _with_video_media(album_dir, recorded)

    assert tagger.tag_album(album_dir, _release_cd_plus_dvd()) == 2


# ---------- the dry run (#266) ----------


def test_plan_reports_every_field_a_first_tagging_would_set(album_with_tracks):
    """An untagged album's plan names each file and, within it, every owned field
    the tagging would fill — the same vocabulary the audit record and the undo
    use, because both come from `owned.diff` over the same values."""
    album_dir = album_with_tracks(2)

    plan = tagger.plan_album(album_dir, _release_2_tracks())

    assert not plan.empty
    assert set(plan.changes) == {album_dir / "01 Track 1.m4a", album_dir / "02 Track 2.m4a"}
    first = plan.changes[album_dir / "01 Track 1.m4a"]
    assert first["album"] == [None, "Test Album"]
    assert first["title"] == [None, "Track 1"]
    assert first["mb_album_id"] == [None, "rel-aaa"]
    # Every key is an owned field's name, not a display label.
    assert set(first) <= {f.value for f in owned.Owned}


def test_plan_of_an_already_tagged_album_is_empty(album_with_tracks):
    """The idempotency invariant #32 rests on: a second look at an unchanged
    MusicBrainz release finds nothing to do."""
    album_dir = album_with_tracks(2)
    rel = _release_2_tracks()
    tagger.tag_album(album_dir, rel)

    assert tagger.plan_album(album_dir, rel).empty


def test_plan_writes_nothing(album_with_tracks):
    """A dry run is dry: no tags written, and no file touched. The mtime is the
    half that matters — `reconcile.looks_externally_retagged` compares it against
    the sidecar's `tagged_at`, so a plan that bumped it would make every album it
    inspected look re-tagged by someone else (#220)."""
    album_dir = album_with_tracks(2)
    f = album_dir / "01 Track 1.m4a"
    os.utime(f, (1_000_000, 1_000_000))
    before = dict(MP4(f))

    tagger.plan_album(album_dir, _release_2_tracks())

    assert dict(MP4(f)) == before
    assert f.stat().st_mtime == 1_000_000


def test_plan_reports_only_the_field_musicbrainz_moved(album_with_tracks):
    """The detector's real job: after a tagging, one corrected value upstream
    shows up as one field on one file, not as a whole-album rewrite."""
    album_dir = album_with_tracks(2)
    rel = _release_2_tracks()
    tagger.tag_album(album_dir, rel)

    rel["medium-list"][0]["track-list"][0]["title"] = "Track One"
    plan = tagger.plan_album(album_dir, rel)

    assert plan.changes == {album_dir / "01 Track 1.m4a": {"title": ["Track 1", "Track One"]}}


def test_plan_raises_when_the_release_no_longer_fits_the_files(album_with_tracks):
    """A changed track count is refused here exactly as it is at tagging time, so
    the gardener learns the album's structure moved without attempting a write.
    That IS the finding: structure is a question for a human, not something to
    auto-apply (#32)."""
    album_dir = album_with_tracks(3)

    with pytest.raises(TagMismatchError):
        tagger.plan_album(album_dir, _single_track_release())


def test_plan_reports_the_artwork_it_would_embed(album_with_tracks, tmp_path):
    """Artwork rides alongside the owned fields under `owned.ARTWORK`, as it does
    in the audit record. Without it a "dry run" would report no change on an album
    whose cover a tagging is about to replace."""
    album_dir = album_with_tracks(2)
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(_minimal_jpeg())

    plan = tagger.plan_album(album_dir, _release_2_tracks(), cover_path=cover)

    was, now = plan.changes[album_dir / "01 Track 1.m4a"][owned.ARTWORK]
    assert was is None
    assert now == hashlib.sha256(_minimal_jpeg()).hexdigest()


def test_plan_reports_per_track_artwork_it_would_preserve(album_with_tracks, tmp_path):
    """The dry run reaches the same DATA SAFETY verdict a tagging does — keep the
    per-track images, don't embed the album cover — and reports it instead of
    logging it. #272 needs that: the decision recurs identically on every pass, so
    only the pass that actually writes may announce it."""
    album_dir = album_with_tracks(2)
    _embed_cover(album_dir / "01 Track 1.m4a", _minimal_jpeg())
    _embed_cover(album_dir / "02 Track 2.m4a", _minimal_jpeg() + b"_different")
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(_minimal_jpeg())

    plan = tagger.plan_album(album_dir, _release_2_tracks(), cover_path=cover)

    assert plan.preserves_per_track_art
    assert not any(owned.ARTWORK in c for c in plan.changes.values())


# ---------- a tagging that would change nothing writes nothing (#266) ----------


def test_retag_leaves_a_file_it_would_not_change_alone(album_with_tracks):
    """The idempotency invariant made mechanical rather than merely recorded. A
    re-tag used to rewrite every file regardless, which bumps the mtime that
    `reconcile.looks_externally_retagged` reads as "somebody else tagged this"
    (#220) — and under #32's nightly pass it would do that to the whole library
    every night."""
    album_dir = album_with_tracks(2)
    rel = _release_2_tracks()
    tagger.tag_album(album_dir, rel)
    f = album_dir / "01 Track 1.m4a"
    os.utime(f, (1_000_000, 1_000_000))

    tagger.tag_album(album_dir, rel)

    assert f.stat().st_mtime == 1_000_000


def test_retag_records_the_album_line_but_no_track_line_when_nothing_changed(
    album_with_tracks, tmp_path
):
    """ "The pass ran and found the files already correct" is a different fact from
    "the pass never ran", so `tag.album` still lands. A `tag.track` line does not:
    it would claim a file was written, and its per-field detail — which is what a
    revert reads — would be empty."""
    from harmonist import activity_store
    from harmonist.activity_store import Source

    activity_store.init(tmp_path / "audit.db")
    album_dir = album_with_tracks(2)
    rel = _release_2_tracks()
    tagger.tag_album(album_dir, rel)
    before = len(_detail())

    tagger.tag_album(album_dir, rel)

    rows = [e.message for e in activity_store.recent(50, source=Source.AUDIT)]
    assert len([m for m in rows if m.startswith("tag.album")]) == 2
    assert len([m for m in rows if m.startswith("tag.track")]) == 2  # both from the first pass
    assert len(_detail()) == before


def test_retag_returns_the_file_count_even_when_it_writes_nothing(album_with_tracks):
    """The count is how many files the album is now correctly tagged as, which is
    what the caller reports to the user — not how many needed touching."""
    album_dir = album_with_tracks(2)
    rel = _release_2_tracks()
    tagger.tag_album(album_dir, rel)

    assert tagger.tag_album(album_dir, rel) == 2


def test_retag_still_clears_a_legacy_atom_on_an_otherwise_unchanged_file(album_with_tracks):
    """The one thing an owned-field diff cannot see. `read_owned` reports the
    canonical `MusicBrainz Album Id`; a write ALSO clears the legacy
    `MUSICBRAINZ_RELEASEID` spelling beside it. So a file can match the release on
    all thirty owned fields and still carry a stale MBID under the old name —
    exactly the shape of an album adopted from an older Picard, which is the case
    #32's whole premise rests on. Skipping the write on the diff alone would leave
    it there forever."""
    album_dir = album_with_tracks(1)
    f = album_dir / "01 Track 1.m4a"
    tagger.tag_album(album_dir, _single_track_release())
    audio = MP4(f)
    audio[LEGACY_RELEASE_ID] = [b"old-broken-mbid"]
    audio.save()

    tagger.tag_album(album_dir, _single_track_release())

    assert LEGACY_RELEASE_ID not in MP4(f)


# ---------- which changes may apply unattended (#267) ----------


def test_a_detail_musicbrainz_filled_in_is_enrichment():
    """Nothing about what the album is, or how it is laid out, moves."""
    assert (
        tagger.significance_of("catalog_number", None, "CAT-001") is owned.Significance.ENRICHMENT
    )
    assert tagger.significance_of("isrcs", [], ["GBTEST2100001"]) is owned.Significance.ENRICHMENT
    assert tagger.significance_of("date", "2021", "2021-06-15") is owned.Significance.ENRICHMENT


def test_what_the_album_is_reads_as_identity():
    for field, was, now in [
        ("album", "Geogaddi", "Geogaddi (Remastered)"),
        ("album_artist", "Boards Of Canada", "Boards of Canada"),
        ("mb_album_id", "rel-aaa", "rel-bbb"),
        ("mb_track_id", "rec-001", "rec-999"),
    ]:
        assert tagger.significance_of(field, was, now) is owned.Significance.IDENTITY, field


def test_how_the_album_is_laid_out_reads_as_structure():
    """The same album, renumbered — distinct from the album becoming a different
    album, which is what separating the two levels is for."""
    for field, was, now in [
        ("track_num", 3, 4),
        ("track_total", 12, 13),
        ("disc_num", 1, 2),
        ("disc_total", 1, 2),
    ]:
        assert tagger.significance_of(field, was, now) is owned.Significance.STRUCTURE, field


def test_a_title_tidy_up_is_cosmetic_but_a_retitle_is_identity():
    """The one classification a field-level map cannot give.

    Whitespace and casing are the line `models.norm_title` already draws, and the
    album page draws it in the same place — a title it reports as unchanged must
    not be one the gardener treats as a retitle.
    """
    assert (
        tagger.significance_of("title", "Dawn  Chorus", "Dawn Chorus")
        is owned.Significance.COSMETIC
    )
    assert (
        tagger.significance_of("title", "DAWN CHORUS", "Dawn Chorus") is owned.Significance.COSMETIC
    )
    assert (
        tagger.significance_of("title", "Dawn Chorus", "Dawn Chorus (Alt. Take)")
        is owned.Significance.IDENTITY
    )


def test_a_mark_respelt_in_another_typeface_is_cosmetic():
    """#379: the same reading the album page makes, since they share `norm_title`.

    Kept beside the whitespace/casing case rather than folded into it: that one
    is about the line existing at all, this is about where it now falls. The
    third assertion is the load-bearing one — a rule that dropped punctuation
    instead of canonicalising it would call a retitle cosmetic, and under #273's
    setting that is a write nobody agreed to.
    """
    assert (
        tagger.significance_of("title", "Humanity's Shadow", "Humanity’s Shadow")
        is owned.Significance.COSMETIC
    )
    assert (
        tagger.significance_of("title", "Blue - Green", "Blue — Green")
        is owned.Significance.COSMETIC
    )
    assert tagger.significance_of("title", "Live?", "Live!") is owned.Significance.IDENTITY


def test_a_title_arriving_or_leaving_is_not_cosmetic():
    """A field appearing or vanishing is a real change however the strings would
    have normalised, so it falls through to the REVIEW the map already gave."""
    assert tagger.significance_of("title", None, "Dawn Chorus") is owned.Significance.IDENTITY
    assert tagger.significance_of("title", "Dawn Chorus", None) is owned.Significance.IDENTITY


def test_a_credit_list_arriving_is_enrichment_but_a_changed_one_is_identity():
    """#389: the second kind of by-value rule, and the reason the field-level
    table cannot answer for these two.

    `albumartists` and `artists` are new in Picard `3.0.0rc1` as well as here, so
    no library predating it carries either — and one IDENTITY entry ranked every
    collaboration in a real library above a genuine retitle. A list *arriving* is
    MusicBrainz filling in a detail; a list whose names have *moved* is what the
    album, or the track, IS.
    """
    for field in ("album_artists", "artists"):
        assert (
            tagger.significance_of(field, None, ["Hauschka", "Hildur Guðnadóttir"])
            is owned.Significance.ENRICHMENT
        ), field
        assert (
            tagger.significance_of(field, [], ["Hauschka", "Hildur Guðnadóttir"])
            is owned.Significance.ENRICHMENT
        ), field
        assert (
            tagger.significance_of(field, ["Hauschka"], ["Hauschka", "Hildur Guðnadóttir"])
            is owned.Significance.IDENTITY
        ), field
        assert (
            tagger.significance_of(field, ["Somebody Else"], ["Hauschka"])
            is owned.Significance.IDENTITY
        ), field


def test_a_credit_list_being_emptied_is_not_an_enrichment():
    """The mirror of the title case: a field vanishing is a real change however
    small the values look, so it falls through to the level the map gave. Losing
    the only record of where one artist ends and the next begins is not
    MusicBrainz filling in a detail."""
    assert (
        tagger.significance_of("album_artists", ["Hauschka", "Hildur Guðnadóttir"], [])
        is owned.Significance.IDENTITY
    )


def test_every_by_value_field_has_a_rule_deciding_when_it_lowers():
    """The two halves of a by-value field live in different modules — `owned`
    declares how far it may drop, this one holds the comparison that decides,
    because only this side can reach `models.norm_title` — so nothing but this
    keeps them in step. A rule keyed to no declaration is dead code, and a
    declaration with no rule raises at classification time, which is a `KeyError`
    out of the gardener's pass rather than a field quietly staying at IDENTITY.

    The direction those two tables must run in is asserted where both of them
    can be read at once: `test_a_value_sensitive_field_is_declared_at_its_higher_significance`.
    """
    assert set(owned.BY_VALUE) == set(tagger.LOWERED_WHEN)


def test_artwork_is_its_own_level():
    """Artwork arrives in a plan's changes under a key that is deliberately not
    an owned field, and it still has to be classified — `owned.ARTWORK` is in the
    table so the classifier can't meet a key it has no answer for.

    Its own level rather than a rank among the others because "let it update my
    cover art" is a trust decision people make separately from anything about
    tags, and #273's setting is per level.
    """
    assert tagger.significance_of(owned.ARTWORK, "sha-a", "sha-b") is owned.Significance.COVER_ART


def test_an_unknown_key_is_refused_rather_than_guessed():
    """No default. A plan cannot produce a key outside the map, and inventing one
    here is how a field would quietly acquire a significance nobody gave it —
    which is the failure the totality test exists to prevent, defeated at the
    reader instead of at the map."""
    with pytest.raises(KeyError):
        tagger.significance_of("genre", None, "Ambient")


def test_every_change_reaching_the_runner_still_goes_to_review():
    """The policy today, asserted through the pair of calls the runner will make.

    `significance_of` says what a change is; `needs_review` says what to do about
    it, and for now the answer is the same for every level. Asserted here as well
    as over the enum because this is the shape #270 will actually use, and the
    two halves being separable is the whole point of the correction that split
    them (#273 attaches a per-level setting to the second one).
    """
    for field, was, now in [
        ("catalog_number", None, "CAT-001"),
        ("title", "Dawn  Chorus", "Dawn Chorus"),
        ("album", "A", "B"),
        ("track_num", 1, 2),
        (owned.ARTWORK, "sha-a", "sha-b"),
    ]:
        assert owned.needs_review(tagger.significance_of(field, was, now)), field


def test_every_change_a_real_plan_produces_can_be_classified(album_with_tracks, tmp_path):
    """The map and the diff must agree about the vocabulary they share.

    The totality test asserts that over `Owned` as declared; this asserts it over
    what a tagging actually emits, which is the thing the runner will iterate. A
    plan that produced a key the map had never heard of would pass the first test
    and raise in production (#270).
    """
    album_dir = album_with_tracks(2)
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(_minimal_jpeg())

    plan = tagger.plan_album(album_dir, _release_2_tracks(), cover_path=cover)

    seen = {f for changes in plan.changes.values() for f in changes}
    assert owned.ARTWORK in seen  # the key most likely to be forgotten
    for field in seen:
        assert isinstance(tagger.significance_of(field, None, "x"), owned.Significance)


# ---------- Picard's disambiguated album title (#283) ----------


def _set_album_tag(album_dir, value: str) -> None:
    """Put `value` in every file's ©alb — what Picard writes with its
    "use release disambiguation in album title" option turned on."""
    for f in sorted(album_dir.glob("*.m4a")):
        audio = MP4(f)
        audio[ATOM_ALBUM] = [value]
        audio.save()


def test_a_disambiguated_album_title_is_not_a_change(album_with_tracks):
    """Picard can be told to append the release disambiguation to the album
    title, so a library tagged that way carries `Test Album (expanded edition)`
    where MusicBrainz's release title is `Test Album`.

    That is the same album, by the user's own deliberate setting. Reported as a
    change it would differ on every pass forever: #266's write-skip would never
    fire on those albums, and #267 classifies `album` as IDENTITY, so #32's
    nightly pass would put the whole library in the Inbox on its first night.
    """
    album_dir = album_with_tracks(2)
    rel = _release_2_tracks()
    rel["disambiguation"] = "expanded edition"
    tagger.tag_album(album_dir, rel)
    _set_album_tag(album_dir, "Test Album (expanded edition)")

    assert tagger.plan_album(album_dir, rel).empty


def test_a_real_retitle_is_still_a_change(album_with_tracks):
    """The tolerance is exactly one string, not "anything in brackets".

    A parenthetical that isn't the release's disambiguation is a different album
    title, and guessing otherwise would be inventing an identity the release can
    state for free — which review-gate item 2 forbids.
    """
    album_dir = album_with_tracks(2)
    rel = _release_2_tracks()
    rel["disambiguation"] = "expanded edition"
    tagger.tag_album(album_dir, rel)
    _set_album_tag(album_dir, "Test Album (deluxe edition)")

    plan = tagger.plan_album(album_dir, rel)
    assert not plan.empty
    assert all("album" in c for c in plan.changes.values())


def test_a_bracketed_suffix_is_a_change_when_the_release_has_no_disambiguation(album_with_tracks):
    """With nothing to append there is no second spelling to accept, so a
    bracketed suffix is just a different album title.

    A behaviour assertion, not a test of the None-return in
    `title_with_disambiguation` — a broken guard there yields `Test Album ()`,
    which matches nothing either, and this would stay green. That guard is
    covered directly in `test_title_match.py`. What this does cover is the
    tagger reaching for a looser rule: swap the exact comparison for
    `titles_match` and it fails.
    """
    album_dir = album_with_tracks(2)
    rel = _release_2_tracks()
    tagger.tag_album(album_dir, rel)
    _set_album_tag(album_dir, "Test Album (expanded edition)")

    assert not tagger.plan_album(album_dir, rel).empty


def test_mbid_names_pairs_every_id_the_panel_shows_with_its_name():
    """#298: the album page renders `mb_album_artist_ids` and
    `mb_release_group_id` as names rather than hex, and the name has to come out
    of the same corner of the same payload the id does — read it from anywhere
    else and a row can show one artist's name over another artist's id.

    Walks the bare join-phrase strings musicbrainzngs puts between the artist
    dicts (#183). A walker that doesn't is the recurring bug in this file, and
    here it would raise rather than merely mis-join.
    """
    release = {
        "id": "rel-1",
        "artist-credit": [
            {"artist": {"id": "art-1", "name": "zakè"}},
            " & ",
            {"artist": {"id": "art-2", "name": "rhubiqs"}},
        ],
        "release-group": {"id": "rg-1", "title": "Ausência"},
    }

    assert tagger.mbid_names(release) == {
        "art-1": "zakè",
        "art-2": "rhubiqs",
        "rg-1": "Ausência",
    }
    # The release's own id is deliberately absent: its comparison row is gone,
    # and nothing renders it as a link (#298).
    assert "rel-1" not in tagger.mbid_names(release)


def test_mbid_names_is_empty_rather_than_partial_on_a_thin_release():
    """A release with no artist-credit and no release group yields no names at
    all, and the page falls back to the raw ids — which is the honest answer.
    Returning an entry with an empty name would render a link with no text."""
    assert tagger.mbid_names({"id": "rel-1"}) == {}
    assert tagger.mbid_names({"artist-credit": [{"artist": {"id": "art-1"}}]}) == {}


def _featured_release() -> dict:
    """*A Fragile Geography* in miniature: an album credited to one artist, with
    one track credited to two — which is where a guest actually lives, since
    MusicBrainz's style moves them out of the track title and into the credit."""
    return {
        "id": "rel-1",
        "title": "A Fragile Geography",
        "artist-credit": [{"artist": {"id": "art-1", "name": "Rafael Anton Irisarri"}}],
        "release-group": {"id": "rg-1", "title": "A Fragile Geography"},
        "medium-list": [
            {
                "position": "1",
                "track-list": [
                    {"id": "rt-1", "position": "1", "title": "Hiatus"},
                    {
                        "id": "rt-2",
                        "position": "2",
                        "title": "Empire Systems",
                        "artist-credit": [
                            {"artist": {"id": "art-1", "name": "Rafael Anton Irisarri"}},
                            " feat. ",
                            {"artist": {"id": "art-2", "name": "Julia Kent"}},
                        ],
                    },
                ],
            }
        ],
    }


def test_mbid_names_reaches_the_artists_credited_on_a_track():
    """The gap #309 names: `mbid_names` walked the RELEASE credit and stopped, so
    on this very album the guest's artist id had no name available anywhere on
    the page — and the tracklist now gives that id a column."""
    names = tagger.mbid_names(_featured_release())

    assert names["art-2"] == "Julia Kent", "credited on a track, not on the release"
    assert names["art-1"] == "Rafael Anton Irisarri"


def test_a_credit_is_keyed_by_the_phrase_the_tagger_writes():
    """What makes it safe to draw a credit as its parts (#309): the key IS the
    flat string written to `artist`, so a credit can only ever be applied to a
    value it spells character for character.

    Asserted against `_build_tagset`'s own output rather than a literal, because
    a phrase built one way here and another way there is precisely the failure —
    the page would replace the user's tag with different words and call it the
    same value.
    """
    release = _featured_release()
    credits = tagger.artist_credits(release)
    written = {t.artist for t in tagger.tagsets_for(release)}

    assert written <= set(credits), "every artist phrase tagging writes is a key"
    parts = credits["Rafael Anton Irisarri feat. Julia Kent"]
    assert [(p.name, p.mbid, p.join) for p in parts] == [
        ("Rafael Anton Irisarri", "art-1", " feat. "),
        ("Julia Kent", "art-2", ""),
    ]
    assert "".join(p.name + p.join for p in parts) == "Rafael Anton Irisarri feat. Julia Kent"


def test_the_written_phrase_is_exactly_what_the_parts_spell():
    """`_artist_phrase` writes a TAG, and #309 rebuilt it on top of `_credit_parts`
    so the flat string and the linked parts cannot disagree. That rebuild is only
    safe if it reproduces the old walk character for character — a payload shape
    the two handled differently would change what lands in the user's file.

    Both spellings of a join phrase are covered: the bare string element
    musicbrainzngs actually emits, and the `joinphrase` key the JSON service uses
    (#183). So is a credit that opens with a bare string, which is malformed and
    is carried through rather than swallowed.
    """
    from harmonist.tagger import _artist_phrase, _credit_parts

    def before_309(artist_credit):
        """The walk `_artist_phrase` was, verbatim.

        Written out here rather than asserted against `_credit_parts`, which
        would be circular — `_artist_phrase` is now built from it, so comparing
        the two can only ever agree. This is the reference the rewrite has to
        match, and it is the only thing in this file that can say it does.
        """
        if not artist_credit:
            return ""
        parts = []
        for ac in artist_credit:
            if isinstance(ac, str):
                parts.append(ac)
            elif isinstance(ac, dict):
                parts.append(ac.get("name") or ac.get("artist", {}).get("name", ""))
                if jp := ac.get("joinphrase"):
                    parts.append(jp)
        return "".join(parts).strip()

    for credit in (
        [
            {"artist": {"id": "a", "name": "zakè"}},
            " & ",
            {"artist": {"id": "b", "name": "rhubiqs"}},
        ],
        [{"artist": {"id": "a", "name": "zakè"}, "joinphrase": " feat. "}, {"name": "rhubiqs"}],
        # The credited-as name wins over the artist's own, which is what makes the
        # parts spell the phrase rather than a corrected version of it.
        [{"name": "Prince", "artist": {"id": "a", "name": "The Artist"}}],
        ["presenting ", {"artist": {"id": "a", "name": "zakè"}}],
        [{"artist": {}, "name": ""}],
        [],
    ):
        assert _artist_phrase(credit) == before_309(credit), credit
        assert "".join(p.name + p.join for p in _credit_parts(credit)).strip() == before_309(credit)


def test_one_phrase_two_different_credits_is_dropped_rather_than_guessed():
    """The design's exact-scoped-unique rule, one level down. Two artists sharing
    a spelling is ambiguity, and picking the first would link one artist's name
    to the other's page — rendering it flat loses a link, guessing states
    something false."""
    release = {
        "id": "rel-1",
        "artist-credit": [{"artist": {"id": "art-1", "name": "Nova"}}],
        "medium-list": [
            {
                "position": "1",
                "track-list": [
                    {
                        "id": "rt-1",
                        "position": "1",
                        "title": "One",
                        "artist-credit": [{"artist": {"id": "art-2", "name": "Nova"}}],
                    }
                ],
            }
        ],
    }

    assert tagger.artist_credits(release) == {}


def _set_country_tag(album_dir, value: str) -> None:
    """Put `value` in every file's release-country atom — what Picard writes
    when `preferred_release_countries` names one of the release's own."""
    for f in sorted(album_dir.glob("*.m4a")):
        audio = MP4(f)
        audio[ATOM_MB_ALBUM_COUNTRY] = [value.encode("utf-8")]
        audio.save()


def _multi_country_2_tracks() -> dict:
    """`_release_2_tracks`, issued in three countries instead of one."""
    return _release_2_tracks() | {
        "release-event-list": [
            {
                "date": "2021-06-15",
                "area": {"name": "United Kingdom", "iso-3166-1-code-list": ["GB"]},
            },
            {"date": "2021-06-18", "area": {"name": "Germany", "iso-3166-1-code-list": ["DE"]}},
            {"date": "2021-06-22", "area": {"name": "Japan", "iso-3166-1-code-list": ["JP"]}},
        ],
    }


def test_a_second_release_country_is_not_a_change(album_with_tracks):
    """Picard writes whichever of the release's countries `preferred_release_
    countries` names, so a library tagged that way carries "DE" where
    MusicBrainz's scalar `country` is "GB" (`picard/mbjson.py`,
    `release_to_metadata`).

    Both are countries THIS release was issued in, so the file is not out of
    date. Reported as a change it would differ on every pass forever — the #283
    shape exactly: #266's write-skip could never fire on those albums, and
    `mb_album_country` is ENRICHMENT, so #32's nightly pass would put every one
    of them in the Inbox.
    """
    album_dir = album_with_tracks(2)
    rel = _multi_country_2_tracks()
    tagger.tag_album(album_dir, rel)
    _set_country_tag(album_dir, "DE")

    assert tagger.plan_album(album_dir, rel).empty


def test_a_country_the_release_never_names_is_still_a_change(album_with_tracks):
    """The tolerance is this release's own release events, not "any country".

    Accepting a code MusicBrainz does not list for the release would be
    inventing a fact it states for free — review-gate item 2. A file carrying
    "US" for a release issued in GB, DE and JP is genuinely stale.
    """
    album_dir = album_with_tracks(2)
    rel = _multi_country_2_tracks()
    tagger.tag_album(album_dir, rel)
    _set_country_tag(album_dir, "US")

    plan = tagger.plan_album(album_dir, rel)
    assert not plan.empty
    assert all("mb_album_country" in c for c in plan.changes.values())


def test_a_single_country_release_accepts_only_that_country(album_with_tracks):
    """With one release event there is no second country to accept, so a
    different code is just a wrong one — and this is the everyday album."""
    album_dir = album_with_tracks(2)
    rel = _release_2_tracks()  # country GB, no release events at all
    tagger.tag_album(album_dir, rel)
    _set_country_tag(album_dir, "DE")

    assert not tagger.plan_album(album_dir, rel).empty


# ---------- release events (#329) ----------


def _multi_country_release() -> dict:
    """*Amok* in miniature: one release, three release events.

    MusicBrainz's own payload for `3587efcb-…`, trimmed — the release named in
    #329. `country` and `date` carry the German event; the other two are
    reachable only through `release-event-list`.
    """
    return {
        "id": "rel-amok",
        "country": "DE",
        "date": "2013-06-07",
        "release-event-list": [
            {
                "date": "2013-06-07",
                "area": {"name": "Germany", "iso-3166-1-code-list": ["DE"]},
            },
            {
                "date": "2013-06-10",
                "area": {"name": "United Kingdom", "iso-3166-1-code-list": ["GB"]},
            },
            {
                "date": "2013-06-11",
                "area": {"name": "United States", "iso-3166-1-code-list": ["US"]},
            },
        ],
    }


def test_release_events_reports_every_country_not_just_the_tagged_one():
    """#329: the album page showed "DE" for a release MusicBrainz issued in
    three countries, because `country` is the only one the tag carries. The
    other two exist in the payload the page already holds."""
    events = tagger.release_events(_multi_country_release())

    assert [(e.country, e.date) for e in events] == [
        ("DE", "2013-06-07"),
        ("GB", "2013-06-10"),
        ("US", "2013-06-11"),
    ]
    assert [e.area for e in events] == ["Germany", "United Kingdom", "United States"]


def test_release_events_marks_the_one_the_tags_come_from():
    """Which event is written is what makes the list an explanation rather than
    a contradiction of the Country row beside it. Read off `country` rather than
    assumed to be the first: MusicBrainz picks it, and this must follow."""
    release = _multi_country_release()
    assert [e.written for e in tagger.release_events(release)] == [True, False, False]

    release["country"] = "US"
    assert [e.written for e in tagger.release_events(release)] == [False, False, True]

    # Nothing is claimed when the two can't be reconciled — better a list with
    # no mark than a mark on the wrong row.
    release["country"] = "JP"
    assert [e.written for e in tagger.release_events(release)] == [False, False, False]


def test_release_events_keeps_a_worldwide_event_and_names_no_country():
    """MusicBrainz spells "worldwide" as a release event with no area at all.
    Dropping it would lose the DATE it carries, which is the other half of what
    this list explains."""
    events = tagger.release_events(
        {"id": "rel-1", "date": "2020-01-01", "release-event-list": [{"date": "2020-01-01"}]}
    )
    assert len(events) == 1
    assert events[0].country is None
    assert events[0].area is None
    assert events[0].date == "2020-01-01"


def test_release_events_is_empty_when_musicbrainz_records_none():
    """No events is a fact, not a failure: the page shows the Country row alone,
    with nothing extra to explain."""
    assert tagger.release_events({"id": "rel-1", "country": "GB"}) == ()
