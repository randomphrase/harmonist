"""Read/write .harmonist.json sidecars atomically."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import activity_store, audit, id_registry, library_index

# Re-exported so existing `from harmonist.sidecar import CURRENT_SCHEMA_VERSION`
# keeps working; it's defined in models.py so Sidecar can default to it.
from .models import (
    CURRENT_SCHEMA_VERSION,
    BandcampInfo,
    MatchCandidate,
    Sidecar,
    TrackComparison,
)

log = logging.getLogger(__name__)

SIDECAR_FILENAME = ".harmonist.json"


class UnsupportedSchemaVersionError(Exception):
    pass


class InvalidSidecarError(Exception):
    pass


def sidecar_path(album_dir: Path) -> Path:
    return album_dir / SIDECAR_FILENAME


def has_sidecar(album_dir: Path) -> bool:
    return sidecar_path(album_dir).exists()


def count_all(music_dir: Path) -> int:
    """Number of `.harmonist.json` sidecars under music_dir."""
    if not music_dir.exists():
        return 0
    return sum(1 for _ in music_dir.rglob(SIDECAR_FILENAME))


def delete_all(music_dir: Path) -> int:
    """Delete every `.harmonist.json` sidecar under music_dir; return the count
    removed. ONLY touches sidecar files — audio and cover art are left alone.
    Albums revert to their tag-derived state on the next scan.
    """
    if not music_dir.exists():
        return 0
    # The most destructive thing Harmonist does — every album's identity, match
    # candidate and purchase link goes at once — and it had NO audit record.
    # One line per sidecar, so the record names exactly which albums lost one;
    # a bare count would say a nuke happened but not what it took.
    removed = 0
    failed = 0
    for p in music_dir.rglob(SIDECAR_FILENAME):
        try:
            existing = None
            try:
                existing = read(p.parent)
            except (OSError, InvalidSidecarError):
                # Only costs us the identity FIELDS on the audit line below; the
                # delete itself still proceeds and is still recorded against the
                # path. Loud because a `sidecar.delete` row that can't say which
                # album lost its identity is the one thing that line is for.
                log.exception(
                    "could not read %s before deleting it — its audit record will "
                    "not name the album's identity",
                    p,
                )
            p.unlink()
            removed += 1
            audit.record(
                "sidecar.delete",
                album_id=None if existing is None else _album_id_of(existing),
                album=p.parent,
                mbid=None if existing is None else existing.mb_release_id,
            )
        except OSError:
            # A sidecar that survived the nuke. Counted and reported, or "Erased N
            # sidecar(s)" would over-claim and the user would believe albums were
            # reset that still carry their identity (#104).
            failed += 1
            log.exception("could not delete sidecar %s", p)
            continue
    if failed:
        log.error("%d sidecar(s) could not be deleted and are still on disk", failed)
    # No music_dir field: every path in the log is already relative to it (#98).
    audit.record("sidecar.delete_all", removed=removed, failed=failed)
    library_index.clear()  # the sidecars are gone; the rescan refills the index
    return removed


def read(album_dir: Path) -> Sidecar | None:
    p = sidecar_path(album_dir)
    if not p.exists():
        return None
    with open(p, encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise InvalidSidecarError(f"sidecar at {p} is not valid JSON: {e}") from e
    return _from_dict(data, source_path=p)


def write(album_dir: Path, sidecar: Sidecar) -> None:
    """Atomic: write to temp, fsync, rename.

    Normalises identity at the persistence boundary so callers don't have
    to remember: if `mb_release_id` is set, drop any stale `temp_uid`;
    otherwise reuse the registry UUID for this path (if any) or mint a
    fresh one. Result: exactly one of `(mb_release_id, temp_uid)` is
    non-null on disk, and the URL stays the same across the NEW →
    sidecar'd transition.
    """
    try:
        old = read(album_dir)  # for the audit diff — best-effort, never blocks the write
    except (OSError, InvalidSidecarError, UnsupportedSchemaVersionError):
        # Still doesn't block the write — but it is NOT free, so it can't be
        # silent. `old` feeds _record_identity_alias, which returns early on None,
        # and that alias is only knowable right now: _normalise_identity is about
        # to erase the superseded id from disk. Losing it orphans the album's
        # pre-tag history and 404s every old link to it, permanently. It also
        # makes the audit below record a `sidecar.create` for what is really a
        # modification (#104).
        old = None
        log.exception(
            "could not read the existing sidecar at %s before rewriting it — if this "
            "write changes the album's identity, the link from its old id is lost",
            album_dir,
        )
    sidecar = _normalise_identity(sidecar, album_dir)
    assert bool(sidecar.mb_release_id) ^ bool(sidecar.temp_uid), (
        f"sidecar identity invariant violated: mb_release_id="
        f"{sidecar.mb_release_id!r}, temp_uid={sidecar.temp_uid!r}"
    )
    target = sidecar_path(album_dir)
    tmp = target.with_suffix(target.suffix + ".tmp")
    payload = _to_dict(sidecar)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)
    _audit_sidecar_change(album_dir, old, sidecar)
    _record_identity_alias(old, sidecar)
    # The ONE place the in-memory index learns of a sidecar change (link, demote,
    # tag, download, reconcile all land here) — so sync-time dedup never re-reads.
    library_index.upsert(album_dir, sidecar)


def unlink(album_dir: Path, sidecar: Sidecar, *, candidate: MatchCandidate | None = None) -> None:
    """Drop the album's MusicBrainz release, sending it back to NEEDS_MBID.

    The one definition of what "unlinked" means, because two paths reach it and
    they disagreed about it before this existed (#166):

    * the **"wrong match" pencil** — this release is wrong, help me pick another;
      the on-disk tags are deliberately left alone until the user re-tags;
    * **undoing the tagging that linked the album** (#158) — the tags have just
      been put back, so the sidecar follows them.

    Everything that describes the release goes: `mb_release_id` and `tagged_at`.
    A `track_count_expected` used to go with them — it once survived a pencil,
    leaving a track count describing a release the sidecar no longer named. That
    whole hazard is gone since #195: the expected count is read from the files'
    own tags, so there is no stored copy to fall out of step with anything.

    The store link (`store_url`, `bandcamp`) stays: which shop it came from is
    not a claim about which MusicBrainz release it is.

    `candidate` is the caller's, because it is the one thing the two paths must
    NOT share. The pencil passes None — re-offering a release the user just
    rejected would undo their own judgement. The undo passes the release it
    unlinked, so Confirm is the one-click way back.

    Writes through `write()`, so identity is normalised to the path-derived
    `temp_uid` and the MBID -> temp_uid alias is recorded — which is what keeps
    the album's history reachable from its new id (#33).
    """
    write(
        album_dir,
        replace(
            sidecar,
            mb_release_id=None,
            tagged_at=None,
            mb_match_candidate=candidate,
        ),
    )


def album_id_for(album_dir: Path) -> str | None:
    """The album's canonical id as recorded ON DISK right now, or None if it has
    no sidecar yet.

    Read this AFTER a mutation, never before: tagging drops the sidecar's
    `temp_uid` in favour of the MBID, so an id captured beforehand is frequently
    already dead (#65). Callers that want to tie a log entry to an album need the
    id that will still resolve afterwards.

    Lives here because identity is the sidecar's business, and callers outside
    the web layer (the reconcile runner) need it too."""
    try:
        sc = read(album_dir)
    except (OSError, InvalidSidecarError, UnsupportedSchemaVersionError):
        # None is also "no sidecar yet", so a caller can't tell these apart —
        # but every caller uses this to stamp `album_id=` on a log entry, and
        # neither propagating (a failed read must not abort the tagging run it
        # is describing) nor guessing an id is better. So: keep the None, make
        # the reason findable. A None here means the entry won't appear on that
        # album's history page (#104).
        log.exception(
            "could not read the sidecar at %s to identify the album — records "
            "written for it now won't appear in its history",
            album_dir,
        )
        return None
    return None if sc is None else _album_id_of(sc)


def _album_id_of(sc: Sidecar) -> str | None:
    """The album's canonical id for this sidecar — mirrors `scanner._album_id`
    (MBID preferred, else temp_uid). `write()` normalises identity before this is
    called, so exactly one is set. Defined locally: scanner imports sidecar, so
    importing it back would be circular."""
    return sc.mb_release_id or sc.temp_uid


def _record_identity_alias(old: Sidecar | None, new: Sidecar) -> None:
    """Durably link an album's superseded id to its new one (#33).

    This write is the ONLY moment the pair is knowable: `_normalise_identity`
    drops `temp_uid` once an MBID lands, so a second later there is nothing on
    disk connecting the two. Without the alias, everything recorded under the old
    id — the album's whole pre-tag history, and any deep link already written into
    the activity feed — is orphaned the instant it's tagged.

    Covers every direction, since it compares canonical ids rather than fields:
    temp_uid -> MBID (tag/reconcile), MBID -> MBID (re-match), MBID -> temp_uid
    (unlink). A create (`old is None`) supersedes nothing.

    The audit trail already notes the change, but only as a message string —
    unqueryable, so it can't answer "what is this album's id now?".
    """
    if old is None:
        return
    old_id, new_id = _album_id_of(old), _album_id_of(new)
    if old_id and new_id and old_id != new_id:
        activity_store.record_alias(old_id, new_id)


def _audit_sidecar_change(album_dir: Path, old: Sidecar | None, new: Sidecar) -> None:
    """Audit a sidecar write that creates a sidecar or changes a LOAD-BEARING
    field. No-op re-writes don't log.

    Load-bearing means "changes what the scanner derives, or records a decision
    the user can't easily undo":

      * identity — MBID / Bandcamp item_id / store_url
      * `purchase_unavailable` — a surrender. Permanent: the scanner then treats
        the album as terminal despite having no purchase link, and no future sync
        re-surrenders it. Named in the review gate; it moved silently until #88.

    Deliberately NOT audited, so the narrowness here is a choice rather than an
    oversight:

      * `mb_match_candidate` — a suggestion, not a decision, and it is rewritten
        on every Recheck. Auditing it would bury real changes in churn.
      * `tagged_at` / `added_at` / `downloaded_at` — bookkeeping timestamps; the
        events they date are audited in their own right (see tagger.tag_album).
      * `notes` — free text with no derived consequence.

    Rows carry the album's id AFTER the write; when the id itself changes (an MBID
    rewrite), the `mbid` field records both sides so the two ids can be linked."""
    new_item = new.bandcamp.item_id if new.bandcamp else None
    album_id = _album_id_of(new)
    if old is None:
        audit.record(
            "sidecar.create",
            album_id=album_id,
            album=album_dir,
            mbid=new.mb_release_id,
            item_id=new_item,
            store_url=new.store_url,
        )
        return
    old_item = old.bandcamp.item_id if old.bandcamp else None
    # str-valued (not object) so it splats cleanly into audit.record's typed
    # keyword-only params.
    changes: dict[str, str] = {}
    if old.mb_release_id != new.mb_release_id:
        changes["mbid"] = f"{old.mb_release_id}->{new.mb_release_id}"
    if old_item != new_item:
        changes["item_id"] = f"{old_item}->{new_item}"
    if old.store_url != new.store_url:
        changes["store_url"] = f"{old.store_url}->{new.store_url}"
    if old.purchase_unavailable != new.purchase_unavailable:
        changes["purchase_unavailable"] = f"{old.purchase_unavailable}->{new.purchase_unavailable}"
    if changes:
        audit.record("sidecar.update", album_id=album_id, album=album_dir, **changes)


def _normalise_identity(s: Sidecar, album_dir: Path) -> Sidecar:
    """Enforce identity invariant: exactly one of (mb_release_id, temp_uid)
    is non-null. MBID always wins; temp_uid is minted iff there's no MBID.

    The id comes from `id_registry`, which derives it from the album's path —
    so the inbox URL the user interacted with before any sidecar existed stays
    valid across the first write, and stays valid across restarts.

    Nothing is randomly minted here any more. There used to be a
    `or uuid.uuid4().hex` fallback for a registry that could miss, from when
    ids were random UUIDs in a per-process dict; ids became a hash of the path
    in #114 and the branch has been unreachable ever since. A fallback that
    cannot run is worse than none: it suggests an id may be arbitrary when in
    fact it is always derived, which is the property the alias chain leans on.
    """
    if s.mb_release_id:
        if s.temp_uid is None:
            return s
        return replace(s, temp_uid=None)
    if s.temp_uid is None:
        return replace(s, temp_uid=id_registry.peek(album_dir))
    return s


def _to_dict(s: Sidecar) -> dict[str, Any]:
    d: dict[str, Any] = {"schema_version": s.schema_version}
    if s.store_url:
        d["store_url"] = s.store_url
    if s.bandcamp:
        bd: dict[str, Any] = {}
        if s.bandcamp.item_id is not None:
            bd["item_id"] = s.bandcamp.item_id
        if s.bandcamp.band_id is not None:
            bd["band_id"] = s.bandcamp.band_id
        if s.bandcamp.is_private:  # omit the default (False) to keep sidecars lean
            bd["is_private"] = True
        if s.bandcamp.candidate_item_ids:
            bd["candidate_item_ids"] = list(s.bandcamp.candidate_item_ids)
        if bd:  # only include the block when it has content
            d["bandcamp"] = bd
    if s.downloaded_at:
        d["downloaded_at"] = _iso(s.downloaded_at)
    if s.added_at:
        d["added_at"] = _iso(s.added_at)
    if s.mb_release_id:
        d["mb_release_id"] = s.mb_release_id
    if s.temp_uid:
        d["temp_uid"] = s.temp_uid
    if s.mb_match_candidate:
        d["mb_match_candidate"] = _candidate_to_dict(s.mb_match_candidate)
    if s.tagged_at:
        d["tagged_at"] = _iso(s.tagged_at)
    if s.notes is not None:
        d["notes"] = s.notes
    if s.purchase_unavailable:
        d["purchase_unavailable"] = True
    return d


def _candidate_to_dict(c: MatchCandidate) -> dict[str, Any]:
    out: dict[str, Any] = {
        "mb_release_id": c.mb_release_id,
        "confidence": c.confidence,
        "file_count": c.file_count,
        "track_count": c.track_count,
    }
    if c.track_comparisons:
        out["track_comparisons"] = [_comparison_to_dict(tc) for tc in c.track_comparisons]
    if c.proposed_at:
        out["proposed_at"] = _iso(c.proposed_at)
    if c.notes:
        out["notes"] = list(c.notes)
    if c.mistag_owned_url:
        out["mistag_owned_url"] = c.mistag_owned_url
    if c.mistag_owned_label:
        out["mistag_owned_label"] = c.mistag_owned_label
    if c.mistag_owned_disambig:
        out["mistag_owned_disambig"] = c.mistag_owned_disambig
    if c.mistag_tagged_mbid:
        out["mistag_tagged_mbid"] = c.mistag_tagged_mbid
    if c.mistag_tagged_label:
        out["mistag_tagged_label"] = c.mistag_tagged_label
    if c.mistag_tagged_disambig:
        out["mistag_tagged_disambig"] = c.mistag_tagged_disambig
    if c.mistag_release_group_mbid:
        out["mistag_release_group_mbid"] = c.mistag_release_group_mbid
    if c.unmatched_purchase:
        out["unmatched_purchase"] = True
    return out


def _comparison_to_dict(tc: TrackComparison) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if tc.file_name is not None:
        out["file_name"] = tc.file_name
    if tc.file_duration_ms is not None:
        out["file_duration_ms"] = tc.file_duration_ms
    if tc.file_title is not None:
        out["file_title"] = tc.file_title
    if tc.mb_track_title is not None:
        out["mb_track_title"] = tc.mb_track_title
    if tc.mb_track_length_ms is not None:
        out["mb_track_length_ms"] = tc.mb_track_length_ms
    if tc.delta_ms is not None:
        out["delta_ms"] = tc.delta_ms
    return out


def _candidate_from_dict(d: dict[str, Any]) -> MatchCandidate:
    return MatchCandidate(
        mb_release_id=d["mb_release_id"],
        confidence=d["confidence"],
        file_count=int(d["file_count"]),
        track_count=int(d["track_count"]),
        track_comparisons=[
            TrackComparison(
                file_name=tc.get("file_name"),
                file_duration_ms=tc.get("file_duration_ms"),
                file_title=tc.get("file_title"),
                mb_track_title=tc.get("mb_track_title"),
                mb_track_length_ms=tc.get("mb_track_length_ms"),
                delta_ms=tc.get("delta_ms"),
            )
            for tc in d.get("track_comparisons", [])
        ],
        proposed_at=_parse_iso(d.get("proposed_at")),
        notes=list(d.get("notes", [])),
        mistag_owned_url=d.get("mistag_owned_url"),
        mistag_owned_label=d.get("mistag_owned_label"),
        mistag_owned_disambig=d.get("mistag_owned_disambig"),
        mistag_tagged_mbid=d.get("mistag_tagged_mbid"),
        mistag_tagged_label=d.get("mistag_tagged_label"),
        mistag_tagged_disambig=d.get("mistag_tagged_disambig"),
        mistag_release_group_mbid=d.get("mistag_release_group_mbid"),
        unmatched_purchase=bool(d.get("unmatched_purchase", False)),
    )


def _from_dict(d: dict[str, Any], source_path: Path) -> Sidecar:
    sv = d.get("schema_version")
    if sv != CURRENT_SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            f"sidecar at {source_path} has schema_version={sv}, expected "
            f"{CURRENT_SCHEMA_VERSION}. Delete the sidecar and re-reconcile."
        )

    bandcamp = None
    if "bandcamp" in d:
        bd = d["bandcamp"]
        try:
            item_id_raw = bd.get("item_id")
            item_id = int(item_id_raw) if item_id_raw is not None else None
            cand_raw = bd.get("candidate_item_ids")
            candidate_item_ids = [int(x) for x in cand_raw] if cand_raw else None
            bandcamp = BandcampInfo(
                item_id=item_id,
                band_id=bd.get("band_id"),
                is_private=bool(bd.get("is_private", False)),
                candidate_item_ids=candidate_item_ids,
            )
        except (KeyError, TypeError, ValueError) as e:
            raise InvalidSidecarError(
                f"sidecar at {source_path} has malformed bandcamp block: {e}"
            ) from e

    candidate = None
    if "mb_match_candidate" in d:
        try:
            candidate = _candidate_from_dict(d["mb_match_candidate"])
        except (KeyError, TypeError, ValueError) as e:
            raise InvalidSidecarError(
                f"sidecar at {source_path} has malformed mb_match_candidate: {e}"
            ) from e

    mb_release_id = d.get("mb_release_id")
    temp_uid = d.get("temp_uid")
    if mb_release_id and temp_uid:
        raise InvalidSidecarError(
            f"sidecar at {source_path} has both mb_release_id and temp_uid "
            f"set; these are mutually exclusive."
        )

    return Sidecar(
        schema_version=sv,
        store_url=d.get("store_url"),
        bandcamp=bandcamp,
        downloaded_at=_parse_iso(d.get("downloaded_at")),
        added_at=_parse_iso(d.get("added_at")),
        mb_release_id=mb_release_id,
        temp_uid=temp_uid,
        mb_match_candidate=candidate,
        tagged_at=_parse_iso(d.get("tagged_at")),
        notes=d.get("notes"),
        purchase_unavailable=bool(d.get("purchase_unavailable", False)),
    )


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str | None) -> datetime | None:
    if s is None:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)
