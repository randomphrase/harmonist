"""Tests for the Picard-compatible tagger."""

from __future__ import annotations

import pytest
from mutagen.mp4 import MP4, MP4Cover

from harmonist import tagger
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
                                "joinphrase": " feat. ",
                            },
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
    assert removals[0].changes["label"][1] is None
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
    assert _atom_str(track1, ATOM_MB_ALBUM_TYPE) == "Album"
    assert _atom_str(track1, ATOM_MB_ALBUM_STATUS) == "official"  # Picard lower-cases status
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


def test_tag_album_incomplete_uses_length_similarity_when_available(
    album_with_tracks,
):
    """The sine.m4a fixture is ~1000ms long. If MB track lengths differ
    sharply, the assignment should pick the closest match — not positional.
    """
    album_dir = album_with_tracks(1)
    release = _release_2_tracks()
    # Track 0 wildly mismatched (10s); track 1 matches (1s) — incomplete-mode
    # should pick track 1 even though positional would have picked track 0.
    release["medium-list"][0]["track-list"][0]["recording"]["length"] = "10000"
    release["medium-list"][0]["track-list"][1]["recording"]["length"] = "1000"
    tagger.tag_album(album_dir, release, incomplete=True)
    audio = MP4(album_dir / "01 Track 1.m4a")
    assert _atom_str(audio, ATOM_MB_TRACK_ID) == "rec-002"


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
