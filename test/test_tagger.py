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


# ---------- undoing a tagging (#157) ----------


def _plan():
    """The revert plan for the tagging that just ran, built the way the route
    builds it — from the stored records, through the shared grouping."""
    from harmonist import tag_history

    return tag_history.revert_plan(_detail())


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

    outcome = tagger.revert_tags(album_dir, _plan(), keep_release_id=None)

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

    outcome = tagger.revert_tags(album_dir, plan, keep_release_id=None)

    assert "album" in outcome.stale
    assert "album" not in outcome.restored
    after = formats.read_owned(f)
    assert after["album"] == "Edited By Hand", "the later edit survived"
    assert after["title"] is None, "everything else still went back"


def test_reverting_keeps_the_release_id_while_the_sidecar_claims_it(album_with_tracks, tmp_path):
    """#158's stated limit, made visible. Removing mb_album_id while the sidecar
    still holds the release derives as TAGGING — a spinner in the Inbox forever
    — so the field is left in place and the outcome says so."""
    from harmonist import activity_store, formats

    activity_store.init(tmp_path / "audit.db")
    album_dir = album_with_tracks(2)
    tagger.tag_album(album_dir, _release_2_tracks())

    outcome = tagger.revert_tags(album_dir, _plan(), keep_release_id="rel-aaa")

    assert outcome.kept_release_id is True
    after = formats.read_owned(next(album_dir.glob("*.m4a")))
    assert after["mb_album_id"] == "rel-aaa", "still linked"
    assert after["album"] is None, "everything else went back"


def test_reverting_twice_is_a_no_op(album_with_tracks, tmp_path):
    """Idempotent, like every other transition here — and it must be, because
    the second click of a double-click would otherwise report a second undo."""
    from harmonist import activity_store, formats

    activity_store.init(tmp_path / "audit.db")
    album_dir = album_with_tracks(2)
    tagger.tag_album(album_dir, _release_2_tracks())
    plan = _plan()

    first = tagger.revert_tags(album_dir, plan, keep_release_id=None)
    snapshot = formats.read_owned(next(album_dir.glob("*.m4a")))
    second = tagger.revert_tags(album_dir, plan, keep_release_id=None)

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
        tagger.revert_tags(album_dir, plan, keep_release_id=None)

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

    tagger.revert_tags(album_dir, _plan(), keep_release_id=None)

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

    tagger.revert_tags(album_dir, _plan(), keep_release_id=None)

    assert formats.read_cover(f) == embedded
