"""Write what MusicBrainz says about an artist into an artist NFO (#981).

Only the mapped elements change. Everything else in the file (the elements
Harmonist doesn't map, comments, attributes, the declaration and the whitespace
between them) is kept byte for byte, because the write edits the text where the
mapped elements sit rather than re-serialising a parsed tree. A tree would keep
the same content but respell it: quotes, empty elements, character references
and indentation would all change, and every write would rewrite the whole file.

That promise is checked rather than assumed. The new text is parsed and compared
with the old outside the mapped elements before anything is written, and the
file is read back afterwards; if it doesn't hold what was meant, the original
goes back.
"""

from __future__ import annotations

import bisect
import codecs
import itertools
import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
from xml.parsers import expat
from xml.sax.saxutils import escape

#: The elements MusicBrainz decides, in the order Kodi writes them. A new
#: element goes after the nearest one before it in this order that the file has.
MAPPED: tuple[str, ...] = (
    "name",
    "musicbrainzartistid",
    "sortname",
    "type",
    "gender",
    "disambiguation",
    "born",
    "formed",
    "died",
    "disbanded",
)

#: How each mapped element is named on a page.
LABELS: dict[str, str] = {
    "name": "Name",
    "musicbrainzartistid": "MusicBrainz ID",
    "sortname": "Sort name",
    "type": "Type",
    "gender": "Gender",
    "disambiguation": "Disambiguation",
    "born": "Born",
    "formed": "Formed",
    "died": "Died",
    "disbanded": "Disbanded",
}

# Kodi's own MusicBrainz scraper puts a person's life span in born and died, and
# a group's in formed and disbanded, and gives any other type no dates.
_PEOPLE = frozenset({"Person", "Character"})
_GROUPS = frozenset({"Group", "Orchestra", "Choir"})

_MARKS = (
    # UTF-32 LE's mark begins with UTF-16 LE's, so the longer marks come first.
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)
_DECLARED = re.compile(rb"""\s*<\?xml[^>]*?encoding\s*=\s*["']([A-Za-z][A-Za-z0-9._-]*)["']""")
_INDENT = "\n    "
_NEW_DOCUMENT = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<artist>{}\n</artist>\n'


class NfoWriteError(Exception):
    """The file can't be written as asked. Nothing was changed, or the original
    was put back."""


class NfoChangedError(NfoWriteError):
    """The file isn't what the change was worked out from any more."""


def mapped(artist: Mapping[str, Any]) -> dict[str, str | None]:
    """What MusicBrainz's `artist` says each element it decides should hold.

    None removes the element: a field MusicBrainz has no value for is removed,
    as an owned tag is. The dates go to the pair the type uses; for any other
    type, or none, the dates aren't MusicBrainz's to decide here, so they are
    left out and left alone, as is the pair a type doesn't use.
    """

    def value(found: object) -> str | None:
        return (str(found).strip() or None) if found is not None else None

    life = artist.get("life-span") or {}
    kind = value(artist.get("type"))
    fields = {
        "name": value(artist.get("name")),
        "musicbrainzartistid": value(artist.get("id")),
        "sortname": value(artist.get("sort-name")),
        "type": kind,
        "gender": value(artist.get("gender")),
        "disambiguation": value(artist.get("disambiguation")),
    }
    if kind in _PEOPLE:
        fields |= {"born": value(life.get("begin")), "died": value(life.get("end"))}
    elif kind in _GROUPS:
        fields |= {"formed": value(life.get("begin")), "disbanded": value(life.get("end"))}
    return fields


def plan(data: bytes, wanted: Mapping[str, str | None]) -> dict[str, tuple[str | None, str | None]]:
    """The fields that would change if the file `data` held `wanted`, as
    {field: (before, after)}, in the order the elements are written.

    Raises `NfoWriteError` when the file can't be written safely: when it
    repeats an element the write decides, or holds other elements inside one,
    since keeping one of them or flattening it would be a guess.
    """
    now = _values(_parse(data), wanted)
    return {
        field: (now[field], wanted[field])
        for field in MAPPED
        if field in wanted and now[field] != wanted[field]
    }


def values(data: bytes, fields: Mapping[str, object]) -> dict[str, str | None]:
    """What the file `data` holds now in each of `fields`, None where absent.
    Raises `NfoWriteError` as `plan` does."""
    return _values(_parse(data), fields)


def apply(data: bytes, wanted: Mapping[str, str | None]) -> bytes:
    """`data` with each of `wanted`'s elements holding its value, and nothing
    else changed. Checked before it is returned, so the bytes are safe to write.
    """
    document = _parse(data)
    changes = {
        field: value
        for field, value in wanted.items()
        if _values(document, {field: None})[field] != value
    }
    if not changes:
        return data
    if document.root is None:
        body = "".join(
            f"{_INDENT}<{field}>{escape(value)}</{field}>"
            for field in MAPPED
            if (value := changes.get(field)) is not None
        )
        result = _NEW_DOCUMENT.format(body).encode("utf-8") if body else data
    else:
        result = document.mark + _edit(document, changes).encode(
            document.codec, errors="xmlcharrefreplace"
        )
    _check(data, result, changes)
    return result


def write(path: Path, *, expected: bytes, wanted: Mapping[str, str | None]) -> bytes:
    """Put `wanted` into the NFO at `path`, which must still hold `expected`.

    Returns what the file holds now. The file is replaced whole, through a
    temporary file beside it, so it is never half written, and read back after;
    a read-back that doesn't match puts `expected` back and raises. Raises
    `NfoChangedError` without writing when the file no longer holds `expected`.
    """
    result = apply(expected, wanted)
    if result == expected:
        return expected
    if _read(path) != expected:
        raise NfoChangedError(f"{path} changed since its changes were worked out")
    _replace(path, result)
    if _read(path) != result:
        _replace(path, expected)
        raise NfoWriteError(f"{path} read back differently after writing; the original is back")
    return result


@dataclass(frozen=True)
class _Element:
    tag: str
    start: int
    end: int
    open_end: int
    #: Where the text between the tags sits; None for an empty-element tag.
    content: tuple[int, int] | None


@dataclass(frozen=True)
class _Document:
    mark: bytes
    codec: str
    text: str
    root: _Element | None
    children: tuple[_Element, ...]
    tree: ET.Element | None


def _parse(data: bytes) -> _Document:
    mark, codec, text = _decode(data)
    if not text.strip():
        return _Document(mark, codec, text, None, (), None)
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise NfoWriteError("DTD and entity declarations are not supported")
    encoded = text.encode("utf-8")
    try:
        root, children = _locate(encoded)
        tree = ET.fromstring(
            encoded,
            parser=ET.XMLParser(
                target=ET.TreeBuilder(insert_comments=True, insert_pis=True), encoding="utf-8"
            ),
        )
    except (expat.ExpatError, ET.ParseError) as exc:
        raise NfoWriteError(f"invalid artist XML ({exc})") from exc
    if root.tag != "artist":
        raise NfoWriteError("expected an <artist> root")
    position = _characters(text, encoded)
    return _Document(
        mark,
        codec,
        text,
        _rebase(root, position),
        tuple(_rebase(child, position) for child in children),
        tree,
    )


def _decode(data: bytes) -> tuple[bytes, str, str]:
    """The byte-order mark, the encoding and the text of `data`."""
    mark, codec = b"", "utf-8"
    for candidate, name in _MARKS:
        if data.startswith(candidate):
            mark, codec = candidate, name
            break
    else:
        if data.startswith(b"<\0?\0"):
            codec = "utf-16-le"
        elif data.startswith(b"\0<\0?"):
            codec = "utf-16-be"
        elif declared := _DECLARED.match(data):
            try:
                codec = codecs.lookup(declared.group(1).decode("ascii")).name
            except LookupError as exc:
                raise NfoWriteError(f"unknown encoding {declared.group(1)!r}") from exc
    try:
        return mark, codec, data[len(mark) :].decode(codec)
    except UnicodeDecodeError as exc:
        raise NfoWriteError(f"not valid {codec}") from exc


def _locate(encoded: bytes) -> tuple[_Element, list[_Element]]:
    """Where the root and each element directly inside it sit, in bytes."""
    parser = expat.ParserCreate(encoding="UTF-8")
    stack: list[tuple[str, int]] = []
    found: list[_Element] = []
    children: list[_Element] = []

    def start(name: str, _attributes: object) -> None:
        stack.append((name, parser.CurrentByteIndex))

    def end(name: str) -> None:
        tag, begin = stack.pop()
        at = parser.CurrentByteIndex
        open_end = _tag_end(encoded, begin)
        if encoded[open_end - 2 : open_end] == b"/>":
            element = _Element(tag, begin, open_end, open_end, None)
        else:
            element = _Element(tag, begin, encoded.index(b">", at) + 1, open_end, (open_end, at))
        if not stack:
            found.append(element)
        elif len(stack) == 1:
            children.append(element)

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.Parse(encoded, True)
    return found[0], children


def _tag_end(encoded: bytes, begin: int) -> int:
    """Just past the `>` closing the tag that starts at `begin`, skipping any
    `>` inside a quoted attribute value."""
    quote = 0
    for index in range(begin, len(encoded)):
        byte = encoded[index]
        if quote:
            if byte == quote:
                quote = 0
        elif byte in b"\"'":
            quote = byte
        elif byte == ord(">"):
            return index + 1
    raise NfoWriteError("unterminated tag")


def _characters(text: str, encoded: bytes) -> Callable[[int], int]:
    """A function from a byte offset in `encoded` to a character offset in `text`:
    the number of characters that end at or before it."""
    if len(text) == len(encoded):
        return lambda offset: offset
    ends = list(itertools.accumulate(len(character.encode("utf-8")) for character in text))
    return lambda offset: bisect.bisect_right(ends, offset)


def _rebase(element: _Element, position: Callable[[int], int]) -> _Element:
    return _Element(
        element.tag,
        position(element.start),
        position(element.end),
        position(element.open_end),
        (position(element.content[0]), position(element.content[1])) if element.content else None,
    )


def _values(document: _Document, fields: Mapping[str, object]) -> dict[str, str | None]:
    if document.tree is None:
        return dict.fromkeys(fields)
    out: dict[str, str | None] = {}
    for field in fields:
        found = document.tree.findall(field)
        if len(found) > 1:
            raise NfoWriteError(f"the file has more than one <{field}>")
        if found and len(found[0]):
            raise NfoWriteError(f"<{field}> holds other elements")
        out[field] = ("".join(found[0].itertext()).strip() or None) if found else None
    return out


def _edit(document: _Document, changes: Mapping[str, str | None]) -> str:
    """The text with each changed element replaced, removed or added."""
    assert document.root is not None
    text = document.text
    by_tag = {child.tag: child for child in document.children}
    edits: list[tuple[int, int, str]] = []
    additions: dict[int, list[str]] = {}
    for field in MAPPED:
        if field not in changes:
            continue
        value = changes[field]
        element = by_tag.get(field)
        if element is not None and value is None:
            # The element and the line it was on, so no blank line is left.
            start = element.start
            while start and text[start - 1] in " \t":
                start -= 1
            if start and text[start - 1] == "\n":
                start -= 1
                if start and text[start - 1] == "\r":
                    start -= 1
            edits.append((start, element.end, ""))
        elif element is not None and value is not None:
            if element.content is None:
                opening = text[element.start : element.open_end - 2].rstrip()
                edits.append(
                    (element.start, element.end, f"{opening}>{escape(value)}</{element.tag}>")
                )
            else:
                edits.append((*element.content, escape(value)))
        elif value is not None:
            anchor = next(
                (
                    by_tag[earlier].end
                    for earlier in reversed(MAPPED[: MAPPED.index(field)])
                    if earlier in by_tag and changes.get(earlier, "") is not None
                ),
                None,
            )
            additions.setdefault(-1 if anchor is None else anchor, []).append(
                f"{_indent(document)}<{field}>{escape(value)}</{field}>"
            )
    root = document.root
    if additions.get(-1):
        added = "".join(additions.pop(-1))
        if root.content is None:
            opening = text[root.start : root.open_end - 2].rstrip()
            edits.append((root.start, root.end, f"{opening}>{added}\n</{root.tag}>"))
        elif not document.children and not text[slice(*root.content)].strip():
            edits.append((*root.content, f"{added}\n"))
        else:
            edits.append((root.open_end, root.open_end, added))
    edits.extend((at, at, "".join(lines)) for at, lines in additions.items())
    pieces: list[str] = []
    position = 0
    for start, end, replacement in sorted(edits, key=lambda edit: (edit[0], edit[1])):
        if start < position:
            raise NfoWriteError("overlapping changes")
        pieces += [text[position:start], replacement]
        position = end
    pieces.append(text[position:])
    return "".join(pieces)


def _indent(document: _Document) -> str:
    """The line break and indentation before the root's first element."""
    root = document.root
    if root is None or root.content is None or not document.children:
        return _INDENT
    before = document.text[root.open_end : document.children[0].start]
    return before if before.strip() == "" and "\n" in before else _INDENT


def _check(before: bytes, after: bytes, changes: Mapping[str, str | None]) -> None:
    """Refuse `after` unless it differs from `before` only in the changed
    elements: every unmapped element, comment and attribute as it was, nothing
    outside the root moved, and each change holding what it should."""
    old, new = _parse(before), _parse(after)
    if new.tree is None:
        raise NfoWriteError("the written file would be empty")
    if old.root is not None and old.tree is not None:
        assert new.root is not None
        if (
            old.text[: old.root.start] != new.text[: new.root.start]
            or old.text[old.root.end :] != new.text[new.root.end :]
            or old.tree.attrib != new.tree.attrib
            or _unmapped(old.tree) != _unmapped(new.tree)
        ):
            raise NfoWriteError("the write would have changed fields Harmonist doesn't map")
    if _values(new, changes) != dict(changes):
        raise NfoWriteError("the write would not have held the values it was meant to")


def _unmapped(root: ET.Element) -> tuple[object, ...]:
    return (
        (root.text or "").strip(),
        tuple(_signature(child) for child in root if child.tag not in MAPPED),
    )


def _signature(element: ET.Element) -> tuple[object, ...]:
    return (
        str(element.tag),
        tuple(sorted(element.attrib.items())),
        element.text,
        tuple(_signature(child) for child in element),
        (element.tail or "").strip(),
    )


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise NfoWriteError(f"Could not read {path}: {exc.strerror}") from exc


_TEMPORARY_SUFFIX = ".tmp"


def is_temporary(path: Path) -> bool:
    """Whether `path` is the file a write goes through on its way to an NFO."""
    return path.name.lower() == "artist.nfo" + _TEMPORARY_SUFFIX


def _replace(path: Path, data: bytes) -> None:
    """Replace `path` with `data` through a temporary file beside it, keeping
    its permissions: another program, a media server perhaps, reads it too."""
    temporary = path.with_name(path.name + _TEMPORARY_SUFFIX)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        with open(temporary, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise NfoWriteError(f"Could not write {path}: {exc.strerror}") from exc
