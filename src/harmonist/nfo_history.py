"""Artist NFO writes as Harmonist records them, and Undo (#917, #981).

Each write records every field it changed, before and after, as a tagging does
(`docs/design/history.md`), and nothing else: no copy of the file. That is what
lets an Undo work after another program has rewritten the file around it. Undo
puts back the fields still holding what the write put there and leaves anything
changed since alone, and is itself a write, recorded the same way.

A write is recorded under the page key the file has afterwards, its artist ID
or the hash of its path, with the key it had before beside it, so the History
of both pages shows it (see the 12->13 migration in `activity_store`).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import activity_store, artists, audit, id_registry, nfo_writer
from .activity_store import Source, StoredEvent
from .tag_history import IdRef

log = logging.getLogger(__name__)

#: The audit event an NFO write records, and that its change detail hangs off.
WRITE_EVENT = "nfo.write"


class UndoUnavailableError(Exception):
    """The write can't be undone: its file has gone, or can't be read."""


@dataclass(frozen=True)
class Written:
    """What one write did: the page the file is on now, and the artist's name
    there, for the Activity feed."""

    key: str
    label: str
    changes: dict[str, tuple[str | None, str | None]]


@dataclass(frozen=True)
class Change:
    """One field of a write, before and after, as History shows it."""

    field: str
    before: str | None
    after: str | None
    #: The names known for the IDs in this row.
    names: tuple[tuple[str, str], ...] = ()

    @property
    def label(self) -> str:
        return nfo_writer.LABELS.get(self.field, self.field)

    @property
    def is_id(self) -> bool:
        return self.field == "musicbrainzartistid"

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
    #: The fields changed since the write, which are left alone.
    kept: tuple[str, ...]


@dataclass(frozen=True)
class HistoryWrite:
    """One write as the artist page's History shows it."""

    event_id: int
    file: str
    changes: tuple[Change, ...]
    undoable: bool


def page_key(path: Path, data: bytes) -> str:
    """The artist page a file holding `data` belongs to, as `Profile.key` says."""
    found = nfo_writer.values(data, {"musicbrainzartistid": None})["musicbrainzartistid"]
    return artists.mbid(found or "") or id_registry.get_or_mint(path)


def label(path: Path, data: bytes) -> str:
    """The artist's name in the file, or its folder's, as the artist list uses."""
    return nfo_writer.values(data, {"name": None})["name"] or path.parent.name


def write(
    path: Path, *, root: Path, expected: bytes, wanted: Mapping[str, str | None]
) -> Written | None:
    """Write `wanted` into the NFO at `path`, which holds `expected`, and record
    each field it changed. None when nothing would change, and then nothing is
    written or recorded: a write that changed nothing has nothing to undo.

    Raises `nfo_writer.NfoWriteError` (and its `NfoChangedError`) from the
    writer, before anything is recorded.
    """
    changes = nfo_writer.plan(expected, wanted)
    if not changes:
        return None
    before = page_key(path, expected)
    after = nfo_writer.write(path, expected=expected, wanted={f: wanted[f] for f in changes})
    key = page_key(path, after)
    event = audit.record(WRITE_EVENT, artist_id=key, file=path, fields=",".join(changes))
    if event is not None:
        activity_store.record_nfo_changes(
            event, file=_relative(path, root), key_before=before, changes=changes
        )
    return Written(key, label(path, after), changes)


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
    return UndoPlan(path, data, revert, tuple(kept))


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
                record.changes, names=_names(record.changes, entry.artist_label if entry else None)
            ),
            undoable=_undoable(record, root),
        )
    return events, writes


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
    changes: Mapping[str, Sequence[str | None]], *, names: Mapping[str, str] | None = None
) -> tuple[Change, ...]:
    """Each field's change, in the order the elements are written."""
    order = {field: index for index, field in enumerate(nfo_writer.MAPPED)}
    return tuple(
        Change(field, before, after, tuple(sorted((names or {}).items())))
        for field, (before, after) in sorted(
            changes.items(), key=lambda item: order.get(item[0], len(order))
        )
    )


def _undoable(record: activity_store.NfoChanges, root: Path) -> bool:
    """Whether Undo would put anything back. A file that has gone or can't be
    written safely offers none: a button that would only fail is worse than
    no button."""
    try:
        return bool(undo_plan(record, root=root).revert)
    except UndoUnavailableError, nfo_writer.NfoWriteError:
        return False


def _relative(path: Path, root: Path) -> str:
    return str(path.relative_to(root))


def _within(root: Path, file: str) -> Path:
    """The recorded file, refused if it isn't an artist NFO inside the library."""
    path = root / file
    if path.name.lower() != "artist.nfo" or not path.resolve().is_relative_to(root.resolve()):
        raise UndoUnavailableError(f"{file} isn't an artist NFO in the library")
    return path
