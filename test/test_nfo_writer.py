"""Writing MusicBrainz's answer into an artist NFO (#981): only the mapped
elements change, and everything else in the file stays byte for byte."""

from __future__ import annotations

import codecs
import stat

import pytest

from harmonist import artist_nfo, nfo_writer
from harmonist.nfo_writer import NfoChangedError, NfoWriteError

MBID = "5b11f4ce-a62d-471e-81fc-a69a8278c7da"

PERSON = {
    "id": MBID,
    "name": "Ada Lovelace",
    "sort-name": "Lovelace, Ada",
    "type": "Person",
    "gender": "Female",
    "life-span": {"begin": "1815-12-10", "end": "1852-11-27", "ended": "true"},
}

GROUP = {
    "id": MBID,
    "name": "The Analytical Engines",
    "sort-name": "Analytical Engines, The",
    "type": "Group",
    "disambiguation": "Victorian computing band",
    "life-span": {"begin": "1843"},
}

KODI = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<!-- written by Kodi -->
<artist>
    <name>Ada</name>
    <genre>Baroque &amp; Roll</genre>
    <!-- keep me -->
    <biography lang='en'>Wrote the first program &#169; 1843.</biography>
    <disambiguation>wrong one</disambiguation>
    <thumb aspect="thumb" preview="a.jpg">a.jpg</thumb>
    <album>
        <title>Notes</title>
        <year>1843</year>
    </album>
</artist>
"""


def test_a_write_changes_only_the_mapped_elements():
    """The file's own spelling survives: the declaration, both comments, the
    single-quoted attribute, the entity and the character reference, the
    nested album, and the indentation. New elements follow the nearest mapped
    element before them, and one MusicBrainz has no value for goes with its line."""
    data = KODI.encode()
    result = nfo_writer.apply(data, nfo_writer.mapped(PERSON))
    assert result.decode() == KODI.replace(
        "    <name>Ada</name>\n",
        "    <name>Ada Lovelace</name>\n"
        f"    <musicbrainzartistid>{MBID}</musicbrainzartistid>\n"
        "    <sortname>Lovelace, Ada</sortname>\n"
        "    <type>Person</type>\n"
        "    <gender>Female</gender>\n"
        "    <born>1815-12-10</born>\n"
        "    <died>1852-11-27</died>\n",
    ).replace("    <disambiguation>wrong one</disambiguation>\n", "")


def test_the_plan_lists_each_change_before_and_after():
    changes = nfo_writer.plan(KODI.encode(), nfo_writer.mapped(PERSON))
    assert changes == {
        "name": ("Ada", "Ada Lovelace"),
        "musicbrainzartistid": (None, MBID),
        "sortname": (None, "Lovelace, Ada"),
        "type": (None, "Person"),
        "gender": (None, "Female"),
        "disambiguation": ("wrong one", None),
        "born": (None, "1815-12-10"),
        "died": (None, "1852-11-27"),
    }


def test_writing_the_same_answer_again_changes_nothing():
    wanted = nfo_writer.mapped(PERSON)
    once = nfo_writer.apply(KODI.encode(), wanted)
    assert nfo_writer.plan(once, wanted) == {}
    assert nfo_writer.apply(once, wanted) is once


def test_the_written_file_reads_as_the_artist(tmp_path):
    path = tmp_path / "artist.nfo"
    path.write_bytes(nfo_writer.apply(KODI.encode(), nfo_writer.mapped(PERSON)))
    profile = artist_nfo.read(path)
    assert (profile.mbid, profile.name, profile.sort_name) == (
        MBID,
        "Ada Lovelace",
        "Lovelace, Ada",
    )


def test_an_empty_file_gets_an_artist_element_holding_the_mapped_fields():
    result = nfo_writer.apply(b"", nfo_writer.mapped(GROUP))
    assert result.decode() == (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        "<artist>\n"
        "    <name>The Analytical Engines</name>\n"
        f"    <musicbrainzartistid>{MBID}</musicbrainzartistid>\n"
        "    <sortname>Analytical Engines, The</sortname>\n"
        "    <type>Group</type>\n"
        "    <disambiguation>Victorian computing band</disambiguation>\n"
        "    <formed>1843</formed>\n"
        "</artist>\n"
    )


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("<artist/>", "<artist>\n    <name>N</name>\n</artist>"),
        ('<artist kind="x" />', '<artist kind="x">\n    <name>N</name>\n</artist>'),
        ("<artist></artist>", "<artist>\n    <name>N</name>\n</artist>"),
        ("<artist>\n</artist>\n", "<artist>\n    <name>N</name>\n</artist>\n"),
        (
            "<artist>\n  <biography>B</biography>\n</artist>",
            "<artist>\n  <name>N</name>\n  <biography>B</biography>\n</artist>",
        ),
    ],
)
def test_an_artist_element_without_mapped_fields_gains_them(before, after):
    assert nfo_writer.apply(before.encode(), {"name": "N"}).decode() == after


def test_an_empty_element_is_filled_and_a_mapped_elements_attributes_are_kept():
    before = '<artist>\n  <name lang="en">Old</name>\n  <type/>\n</artist>'
    result = nfo_writer.apply(before.encode(), {"name": "New", "type": "Group"})
    assert (
        result.decode() == '<artist>\n  <name lang="en">New</name>\n  <type>Group</type>\n</artist>'
    )


def test_values_are_escaped():
    result = nfo_writer.apply(b"<artist><name>x</name></artist>", {"name": "Tom & Jerry <3"})
    assert result == b"<artist><name>Tom &amp; Jerry &lt;3</name></artist>"


def test_positions_after_multibyte_text_land_on_the_right_element():
    before = "<artist>\n  <biography>Café → Ü</biography>\n  <name>Old</name>\n</artist>"
    result = nfo_writer.apply(before.encode(), {"name": "Ñew"})
    assert result.decode() == before.replace("Old", "Ñew")


def test_a_utf16_file_stays_utf16_with_its_mark():
    text = '<?xml version="1.0" encoding="UTF-16"?>\n<artist>\n  <name>Old</name>\n</artist>\n'
    data = codecs.BOM_UTF16_LE + text.encode("utf-16-le")
    result = nfo_writer.apply(data, {"name": "Björk"})
    assert result == codecs.BOM_UTF16_LE + text.replace("Old", "Björk").encode("utf-16-le")


def test_a_character_its_encoding_cannot_hold_is_written_as_a_reference():
    text = '<?xml version="1.0" encoding="ISO-8859-1"?>\n<artist>\n  <name>Café</name>\n</artist>'
    result = nfo_writer.apply(text.encode("latin-1"), {"name": "坂本龍一"})
    assert result == text.replace("Café", "&#22338;&#26412;&#40845;&#19968;").encode("latin-1")


def test_a_repeated_mapped_element_is_not_written():
    data = b"<artist><name>A</name><name>B</name></artist>"
    with pytest.raises(NfoWriteError, match="more than one <name>"):
        nfo_writer.plan(data, nfo_writer.mapped(PERSON))


def test_a_mapped_element_holding_other_elements_is_not_written():
    data = b"<artist><name><first>A</first></name></artist>"
    with pytest.raises(NfoWriteError, match="<name> holds other elements"):
        nfo_writer.apply(data, {"name": "B"})


def test_a_repeated_element_the_write_leaves_alone_does_not_stop_it():
    """Genres repeat in every Kodi file, and Harmonist doesn't map them."""
    data = b"<artist><genre>A</genre><genre>B</genre></artist>"
    result = nfo_writer.apply(data, {"name": "N"})
    assert result == b"<artist>\n    <name>N</name><genre>A</genre><genre>B</genre></artist>"


def test_a_group_gets_formed_and_disbanded_and_keeps_any_born_and_died():
    data = b"<artist>\n  <born>1900</born>\n  <formed>1999</formed>\n</artist>"
    changes = nfo_writer.plan(data, nfo_writer.mapped(GROUP))
    assert changes["formed"] == ("1999", "1843")
    assert "born" not in changes
    assert "disbanded" not in changes  # none on either side


def test_other_artists_get_no_dates_and_keep_theirs():
    other = {**PERSON, "type": "Other"}
    assert not {"born", "died", "formed", "disbanded"} & nfo_writer.mapped(other).keys()
    untyped = {key: value for key, value in PERSON.items() if key != "type"}
    assert not {"born", "died", "formed", "disbanded"} & nfo_writer.mapped(untyped).keys()


def test_a_write_that_would_change_an_unmapped_field_is_refused(monkeypatch):
    """The check is what keeps the promise if the editing is ever wrong."""
    real = nfo_writer._edit

    def careless(document, changes):
        return real(document, changes).replace("<!-- keep me -->", "")

    monkeypatch.setattr(nfo_writer, "_edit", careless)
    with pytest.raises(NfoWriteError, match="fields Harmonist doesn't map"):
        nfo_writer.apply(KODI.encode(), {"name": "Ada Lovelace"})


def test_writing_replaces_the_file_and_keeps_its_permissions(tmp_path):
    path = tmp_path / "artist.nfo"
    path.write_bytes(KODI.encode())
    path.chmod(0o640)
    result = nfo_writer.write(path, expected=KODI.encode(), wanted={"name": "Ada Lovelace"})
    assert path.read_bytes() == result
    assert b"<name>Ada Lovelace</name>" in result
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert sorted(p.name for p in tmp_path.iterdir()) == ["artist.nfo"]


def test_a_file_changed_since_the_plan_is_not_written(tmp_path):
    path = tmp_path / "artist.nfo"
    path.write_bytes(b"<artist><name>Edited elsewhere</name></artist>")
    with pytest.raises(NfoChangedError):
        nfo_writer.write(path, expected=KODI.encode(), wanted={"name": "Ada Lovelace"})
    assert path.read_bytes() == b"<artist><name>Edited elsewhere</name></artist>"


def test_a_write_that_reads_back_wrong_puts_the_original_back(tmp_path, monkeypatch):
    path = tmp_path / "artist.nfo"
    path.write_bytes(KODI.encode())
    real = nfo_writer._replace
    calls: list[bytes] = []

    def flaky(target, data):
        calls.append(data)
        real(target, data[:-5] if len(calls) == 1 else data)

    monkeypatch.setattr(nfo_writer, "_replace", flaky)
    with pytest.raises(NfoWriteError, match="the original is back"):
        nfo_writer.write(path, expected=KODI.encode(), wanted={"name": "Ada Lovelace"})
    assert path.read_bytes() == KODI.encode()


def test_a_write_that_fails_leaves_the_file_and_no_temporary(tmp_path, monkeypatch):
    path = tmp_path / "artist.nfo"
    path.write_bytes(KODI.encode())

    def refuse(src, dst):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(nfo_writer.os, "replace", refuse)
    with pytest.raises(NfoWriteError, match="Permission denied"):
        nfo_writer.write(path, expected=KODI.encode(), wanted={"name": "Ada Lovelace"})
    assert path.read_bytes() == KODI.encode()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["artist.nfo"]
