"""Artist NFO writes as Harmonist records them, and Undo (#917, #981).

Each write records every field it changed, before and after, as a tagging does
(`docs/design/history.md`), and nothing else: no copy of the file. That is what
lets an Undo work after another program has rewritten the file around it. Undo
puts back the fields still holding what the write put there and leaves anything
changed since alone, and is itself a write, recorded the same way.

A write can also put a picture beside the file (#1020), recorded by its name
and digest. Undo removes it while it is still that file, as it puts back a
field still holding what was written, and leaves one replaced since alone.

A write is recorded under the page key the file has afterwards, its artist ID
or the hash of its path, with the key it had before beside it, so the History
of both pages shows it (see the 12->13 migration in `activity_store`).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import activity_store, artist_nfo, artists, audit, id_registry, images, nfo_writer
from .activity_store import Source, StoredEvent
from .tag_history import IdRef

log = logging.getLogger(__name__)

#: The audit event an NFO write records, and that its change detail hangs off.
WRITE_EVENT = "nfo.write"

#: The names a picture is written under: Navidrome's, and Plex reads it too.
_PICTURE_FILES = frozenset({"artist.jpg", "artist.png"})

#: Keeps a failure's detail out of the Activity feed, for one already reported
#: there, or one that drawing a page would report on every redraw.
_LOG_ONLY = {"_activity": True}


class UndoUnavailableError(Exception):
    """The write can't be undone: its file has gone, or can't be read."""


class PictureWriteError(nfo_writer.NfoWriteError):
    """A picture couldn't be put beside the NFO, and nothing else was written."""


@dataclass(frozen=True)
class Written:
    """What one write did: the page the file is on now, and the artist's name
    there, for the Activity feed, and the picture it put beside the file or
    took away, by name."""

    key: str
    label: str
    changes: dict[str, tuple[str | None, str | None]]
    picture: str | None = None
    #: Why a picture the write was given couldn't be put there, when its
    #: fields were written all the same.
    picture_error: str | None = None


@dataclass(frozen=True)
class Credit:
    """Where a field a write filled in came from (#1018): the article, and the
    revision of it whose text was written."""

    url: str
    revision: str


@dataclass(frozen=True)
class PictureCredit:
    """Where a picture a write put beside the file came from (#1020): the
    service, the page to credit it to, and what else its terms ask for.
    fanart.tv's images credit fanart.tv, and the copyright holder where it has
    one recorded; a Commons picture is CC BY or CC BY-SA, which ask for the
    author and the licence."""

    source: str
    url: str
    author: str = ""
    licence: str = ""
    licence_url: str = ""

    @classmethod
    def of(cls, stored: Mapping[str, Any]) -> PictureCredit | None:
        """The credit a stored picture record carries, or None for one that
        names no source, such as a picture an Undo took away."""
        if not stored.get("source") or not stored.get("url"):
            return None
        return cls(
            str(stored["source"]),
            str(stored["url"]),
            str(stored.get("author") or ""),
            str(stored.get("licence") or ""),
            str(stored.get("licence_url") or ""),
        )


@dataclass(frozen=True)
class NewPicture:
    """A picture to put beside an NFO that has none: a JPEG or a PNG, named
    for what it is."""

    data: bytes
    credit: PictureCredit

    @property
    def name(self) -> str:
        return "artist.png" if images.mime_of(self.data) == "image/png" else "artist.jpg"


@dataclass(frozen=True)
class PictureChange:
    """A picture one write put beside the file or took away, as History shows
    it."""

    file: str
    digest: str
    added: bool
    credit: PictureCredit | None = None

    #: Drawn through the same before-and-after pair as a field's change,
    #: which asks whether a row holds IDs.
    is_id = False


@dataclass(frozen=True)
class Change:
    """One field of a write, before and after, as History shows it."""

    field: str
    before: str | None
    after: str | None
    #: The names known for the IDs in this row.
    names: tuple[tuple[str, str], ...] = ()
    #: Where `after` came from, for a field filled in from elsewhere.
    credit: Credit | None = None

    @property
    def label(self) -> str:
        return nfo_writer.LABELS.get(self.field, self.field)

    @property
    def is_id(self) -> bool:
        return self.field == "musicbrainzartistid"

    @property
    def is_filled(self) -> bool:
        """A field filled in from elsewhere, which runs to paragraphs."""
        return self.field in nfo_writer.FILLED

    def parts(self, value: str | None) -> tuple[IdRef | str, ...]:
        """The ID in an ID row's value, named where the name is known."""
        if not value:
            return ()
        return (IdRef(value, dict(self.names).get(value), "artist"),)


@dataclass(frozen=True)
class UndoPlan:
    path: Path
    expected: bytes
    #: What each field still holding what the write put there goes back to.
    revert: dict[str, str | None]
    #: The fields changed since the write, and a picture replaced since, by its
    #: file's name: each is left alone.
    kept: tuple[str, ...]
    #: The picture the write put there, while it is still that file, to
    #: remove, and its digest, which the removal checks again.
    picture: tuple[Path, str] | None = None


@dataclass(frozen=True)
class HistoryWrite:
    """One write as the artist page's History shows it."""

    event_id: int
    file: str
    changes: tuple[Change, ...]
    undoable: bool
    picture: PictureChange | None = None


def page_key(path: Path, data: bytes) -> str:
    """The artist page a file holding `data` belongs to, as `Profile.key` says."""
    found = nfo_writer.values(data, {"musicbrainzartistid": None})["musicbrainzartistid"]
    return artists.mbid(found or "") or id_registry.get_or_mint(path)


def label(path: Path, data: bytes) -> str:
    """The artist's name in the file, or its folder's, as the artist list uses."""
    return nfo_writer.values(data, {"name": None})["name"] or path.parent.name


def write(
    path: Path,
    *,
    root: Path,
    expected: bytes,
    wanted: Mapping[str, str | None],
    credits: Mapping[str, Credit] | None = None,
    picture: NewPicture | None = None,
    remove_picture: tuple[Path, str] | None = None,
) -> Written | None:
    """Write `wanted` into the NFO at `path`, which holds `expected`, and record
    each field it changed, with where `credits` says any of them came from.

    `picture` goes beside the file as an addition (#1020): only while its
    folder still has no picture, looked at again just before, so one that has
    appeared since stands. `remove_picture`, an Undo's file and the digest it
    planned on, is removed only while it is still that file. Either is
    recorded with the fields.

    None when nothing would change, and then nothing is written or recorded: a
    write that changed nothing has nothing to undo.

    Raises `nfo_writer.NfoWriteError` (and its `NfoChangedError`) from the
    writer, before anything is recorded, and `PictureWriteError` when the
    picture couldn't be written and nothing else was. A picture that couldn't
    be written beside fields that were is in the result's `picture_error`.
    """
    changes = nfo_writer.plan(expected, wanted)
    if not changes and picture is None and remove_picture is None:
        return None
    before = page_key(path, expected)
    after = (
        nfo_writer.write(path, expected=expected, wanted={f: wanted[f] for f in changes})
        if changes
        else expected
    )
    placed: dict[str, Any] | None = None
    error: str | None = None
    try:
        if picture is not None:
            placed = _place(path.parent, picture)
        elif remove_picture is not None:
            placed = _remove(path.parent, *remove_picture)
    except OSError as exc:
        verb = "write" if picture is not None else "remove"
        name = (
            picture.name
            if picture is not None
            else remove_picture[0].name
            if remove_picture
            else ""
        )
        log.exception("could not %s %s", verb, path.parent / name)
        error = f"couldn't {verb} {name}: {exc.strerror or exc}"
        if not changes:
            raise PictureWriteError(error) from exc
    if not changes and placed is None:
        return None
    key = page_key(path, after)
    pictured = (
        {"picture": placed["file"], "digest": placed["digest"], "added": placed["added"]}
        if placed
        else {}
    )
    event = audit.record(
        WRITE_EVENT, artist_id=key, file=path, fields=",".join(changes), **pictured
    )
    if event is not None:
        activity_store.record_nfo_changes(
            event,
            file=_relative(path, root),
            key_before=before,
            changes=changes,
            sources={
                field: {"url": credit.url, "revision": credit.revision}
                for field, credit in (credits or {}).items()
                if field in changes
            },
            picture=placed,
        )
    return Written(
        key,
        label(path, after),
        changes,
        picture=placed["file"] if placed else None,
        picture_error=error,
    )


def _place(folder: Path, picture: NewPicture) -> dict[str, Any] | None:
    """Put `picture` in `folder` if it still has none, and return its record:
    None when one has appeared since the lookup, which then stands. The file is
    written whole under another name and moved into place, so a player never
    reads half of it."""
    if artist_nfo.picture_in(folder) is not None:
        return None
    target = folder / picture.name
    temporary = nfo_writer.temporary_for(target)
    try:
        with open(temporary, "wb") as stream:
            stream.write(picture.data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    credit = picture.credit
    return {
        "file": picture.name,
        "digest": images.digest(picture.data),
        "added": True,
        "source": credit.source,
        "url": credit.url,
        **{
            name: value
            for name, value in (
                ("author", credit.author),
                ("licence", credit.licence),
                ("licence_url", credit.licence_url),
            )
            if value
        },
    }


def _remove(folder: Path, target: Path, digest: str) -> dict[str, Any] | None:
    """Remove the picture an Undo planned to, if it is still the file it
    planned on, read again just before: one replaced or removed since stands,
    as a field changed since does. None when there is nothing to remove."""
    if target.parent != folder or target.name.lower() not in _PICTURE_FILES:
        raise UndoUnavailableError(f"{target.name} isn't a picture Harmonist writes")
    try:
        data = target.read_bytes()
    except FileNotFoundError:
        return None
    if images.digest(data) != digest:
        return None
    target.unlink()
    return {"file": target.name, "digest": digest, "added": False}


def undo_plan(record: activity_store.NfoChanges, *, root: Path) -> UndoPlan:
    """What undoing the write `record` describes would put back, from the file
    as it is now. Raises `UndoUnavailableError` when the file has gone or can't
    be read, and `nfo_writer.NfoWriteError` when it can't be written safely."""
    path = _within(root, record.file)
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise UndoUnavailableError(f"{record.file} isn't there any more") from exc
    except OSError as exc:
        raise UndoUnavailableError(f"Could not read {record.file}: {exc.strerror}") from exc
    now = nfo_writer.values(data, record.changes)
    revert: dict[str, str | None] = {}
    kept: list[str] = []
    for field, (before, after) in record.changes.items():
        if now[field] == after:
            revert[field] = before
        elif now[field] != before:
            kept.append(field)
    picture = _picture_to_remove(record.picture, path.parent, kept)
    return UndoPlan(path, data, revert, tuple(kept), picture)


def _picture_to_remove(
    recorded: Mapping[str, Any] | None, folder: Path, kept: list[str]
) -> tuple[Path, str] | None:
    """The picture a write added, while its folder still holds that file, by
    digest. One replaced since is added to `kept` and left alone. A picture an
    Undo took away isn't put back: nothing kept a copy of it, and reading the
    artist again fills it in."""
    if not recorded or not recorded.get("added"):
        return None
    name, digest = str(recorded["file"]), str(recorded["digest"])
    if name.lower() not in _PICTURE_FILES:
        return None
    target = folder / name
    try:
        data = target.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UndoUnavailableError(f"Could not read {name}: {exc.strerror}") from exc
    if images.digest(data) != digest:
        kept.append(name)
        return None
    return target, digest


def history(key: str, *, root: Path) -> tuple[list[StoredEvent], dict[int, HistoryWrite]]:
    """The artist page's History: its events, newest first, and each write's
    changes keyed by the row that shows them.

    A write's changes hang off the Activity entry of the same action, as a
    tagging's do, so they survive hiding the audit rows; a write with no such
    entry keeps them on its own audit row. Raises `StoreUnavailableError`.
    """
    events = activity_store.artist_history(key)
    detail = activity_store.nfo_changes_for([e.id for e in events if e.source is Source.AUDIT])
    entries = {e.action_id: e for e in events if e.source is Source.ACTIVITY and e.action_id}
    writes: dict[int, HistoryWrite] = {}
    for event in events:
        record = detail.get(event.id)
        if record is None:
            continue
        entry = entries.get(event.action_id or "")
        writes[entry.id if entry else event.id] = HistoryWrite(
            event_id=event.id,
            file=record.file,
            changes=changes_of(
                record.changes,
                names=_names(record.changes, entry.artist_label if entry else None),
                credits={
                    field: Credit(source["url"], source["revision"])
                    for field, source in record.sources.items()
                    if "url" in source and "revision" in source
                },
            ),
            undoable=_undoable(record, root),
            picture=(
                PictureChange(
                    str(record.picture["file"]),
                    str(record.picture["digest"]),
                    bool(record.picture.get("added")),
                    PictureCredit.of(record.picture),
                )
                if record.picture
                else None
            ),
        )
    return events, writes


def picture_credit_for(
    writes: Mapping[int, HistoryWrite], picture: Path | None
) -> PictureCredit | None:
    """Where the artist's picture at `picture` came from (#1020): the credit
    of the newest write that put exactly that file there, by name and digest.
    None once it has been replaced since, since it is then no longer the
    picture that was credited, or when it can't be read to tell."""
    added = sorted(
        (w for w in writes.values() if w.picture and w.picture.added and w.picture.credit),
        key=lambda w: w.event_id,
        reverse=True,
    )
    if picture is None or not added:
        return None
    named = [w for w in added if w.picture and w.picture.file == picture.name]
    if not named:
        return None
    try:
        digest = images.digest(picture.read_bytes())
    except OSError:
        # Drawing a page, which the scan has already reported an unreadable
        # picture for: kept out of the Activity feed, or every redraw adds a
        # line to it.
        log.warning("could not read %s to credit it", picture, exc_info=True, extra=_LOG_ONLY)
        return None
    for write in named:
        if write.picture and write.picture.digest == digest:
            return write.picture.credit
    return None


def credit_for(writes: Mapping[int, HistoryWrite], field: str, value: str | None) -> Credit | None:
    """Where the text `value` in `field` came from (#1018): the credit of the
    newest write that put exactly that text there. None once the text has
    changed since, by hand or by another program, since it is then no longer
    the text that was credited."""
    wanted = (value or "").strip()
    if not wanted:
        return None
    for write in sorted(writes.values(), key=lambda w: w.event_id, reverse=True):
        for change in write.changes:
            if change.field == field and change.after == wanted and change.credit:
                return change.credit
    return None


def _names(changes: Mapping[str, Sequence[str | None]], label: str | None) -> dict[str, str]:
    """The names the write itself gives its IDs: the name it wrote beside each,
    or for the ID it left, the name its entry was recorded under."""
    before_id, after_id = changes.get("musicbrainzartistid") or (None, None)
    before_name, after_name = changes.get("name") or (None, None)
    names = {
        id_: name
        for id_, name in ((before_id, before_name), (after_id, after_name or label))
        if id_ and name
    }
    return names


def changes_of(
    changes: Mapping[str, Sequence[str | None]],
    *,
    names: Mapping[str, str] | None = None,
    credits: Mapping[str, Credit] | None = None,
) -> tuple[Change, ...]:
    """Each field's change, in the order the elements are written."""
    order = {field: index for index, field in enumerate(nfo_writer.ELEMENTS)}
    return tuple(
        Change(
            field,
            before,
            after,
            tuple(sorted((names or {}).items())),
            (credits or {}).get(field),
        )
        for field, (before, after) in sorted(
            changes.items(), key=lambda item: order.get(item[0], len(order))
        )
    )


def _undoable(record: activity_store.NfoChanges, root: Path) -> bool:
    """Whether Undo would put anything back. A file that has gone or can't be
    written safely offers none: a button that would only fail is worse than
    no button."""
    try:
        plan = undo_plan(record, root=root)
    except UndoUnavailableError, nfo_writer.NfoWriteError:
        return False
    return bool(plan.revert or plan.picture)


def _relative(path: Path, root: Path) -> str:
    return str(path.relative_to(root))


def _within(root: Path, file: str) -> Path:
    """The recorded file, refused if it isn't an artist NFO inside the library."""
    path = root / file
    if path.name.lower() != "artist.nfo" or not path.resolve().is_relative_to(root.resolve()):
        raise UndoUnavailableError(f"{file} isn't an artist NFO in the library")
    return path
