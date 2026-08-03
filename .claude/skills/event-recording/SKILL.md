---
name: event-recording
description: How Harmonist records what it did — the Activity feed, the audit log, album ids, and action correlation. Consult BEFORE adding or changing any activity.record() / audit.record() call, before writing code that mutates a sidecar, tags, or files on disk, and before changing the wording of an action's outcome. Deciding an operation "is already audited" by grepping for audit.record inside its function is wrong and has been wrong twice; the traps that make event recording quietly incorrect are collected here.
---

# Event recording: activity, audit, ids, correlation

Harmonist has two event sinks with different jobs, one store underneath, and
three identifiers that all move at different times. Every entry below is here
because getting it wrong produced a record that looked fine and was useless.

Schema mechanics live in **`schema-migration`**; whether an operation *needs* a
record at all is **`review-gate`** item 1. This skill is about recording it
*correctly* once you know you should.

## 1. Two sinks — pick deliberately

| | `activity.record()` | `audit.record()` |
|---|---|---|
| Audience | the user, in the Activity tab | forensics — "what exactly did it do?" |
| Voice | plain outcome ("Tagged", "Unlinked") | structured `event key=value` |
| Volume | one per user-visible outcome | as many as it takes |

Both append to `activity_store` tagged by `Source`, so one query returns an
album's activity *and* audit. Warnings and errors logged through the `harmonist`
logger are mirrored into the feed automatically — don't hand-write a duplicate.

`_flash_response()` in `web/main.py` is the funnel for user actions: it writes
the activity entry *and* the status-bar flash from one call.

## 2. Record the album id AFTER the mutation, never before

Album ids **move**. Tagging drops the sidecar's `temp_uid` in favour of the
MBID, and `_normalise_identity` erases the old one — so an id captured before
the write is frequently already dead by the time you record it.

```python
album = _find_album(request, album_id)   # id valid HERE
_tag_with_release(album.path, ...)       # ...and dead by HERE
```

Use `sidecar.album_id_for(album_dir)` after the write. `_live_album_ref()` does
this for `_flash_response`. This shipped broken once (#65): every activity link
pointed at an id that had been erased, and the in-memory `id_registry` fallback
couldn't help because it only knows ids *it* minted — nothing already sidecar'd,
and nothing at all after a restart.

**Watch for a vacuous test here.** The first regression test used an album that
already had an MBID, so its identity never moved and the test passed with the
fix reverted. Mutation-check it: drive a `temp_uid → MBID` transition.

## 3. Identity changes must be recorded as they happen

`sidecar.write()` records an alias when an album's canonical id changes. That
pair is knowable **only** at that instant — afterwards the old id is gone from
disk and nothing can reconstruct it. `_find_album` walks the chain, which is
what keeps an older activity link working.

Anything else derived at write time and unrecoverable later belongs in the same
category: capture it now or lose it permanently.

## 4. Don't put structured data in the message

`album_id` and `album_label` are **columns**. The feed renders the label in its
own position and links it; repeating it in the message shows it twice.

```python
audit.record("sidecar.update", album_id="rel-x", mbid="a->b")
#  -> message "sidecar.update mbid=a->b", album_id in its column
```

Same for user-facing text: `_flash_response`'s `details` should carry the
*reason* ("match found via Recheck"), not the album name.

## 5. Correlation: one action, one id

`activity_store.action()` opens a scope; every event inside — the activity entry
and all audit records beneath it — shares one `action_id`. It is a `ContextVar`
rather than a parameter because `audit.record()` is called from deep inside
`sidecar.py` and `bandcamp_hook.py`, far from anything that knows which user
action is running.

Two thread properties are **load-bearing**, not incidental:

- Starlette copies the context into its threadpool, so a scope opened in HTTP
  middleware **is** visible to a sync `def` route handler.
- A plain `threading.Thread` does **not** inherit it — which is why the sync and
  reconcile runners can't pick up a stale request's id. They open their own.

**Scope per outcome, not per run.** A sync downloading ten albums opens ten
scopes; one run-wide scope would lump every album's records under a single id
and make them individually unrevertible. Nesting keeps the outermost id.

New background work that mutates anything needs its own scope — an
`audit.record()` outside one silently gets NULL, and nothing fails.

## 6. Write the intent record BEFORE a destructive loop

The gate says "before/as it acts". For anything that iterates and mutates, emit
the summary line first, then per-item records as each succeeds:

```python
audit.record("tag.album", release=..., tracks=len(pairs), ...)
for file_path, ... in pairs:
    formats.write_tags(file_path, tagset, cover)
    audit.record("tag.track", file=file_path.name, ...)
```

A crash part-way then leaves evidence of what was attempted rather than silence.
Test it by making the write raise and asserting the summary survived.

## 7. "Is it audited?" — never answer by grepping

This has been wrong twice, in opposite directions:

- **False negative.** `_demote_to_needs_mbid` contains no `audit.record`, yet it
  *is* audited: it clears `mb_release_id`, and `sidecar.write()` audits identity
  changes. Grepping the function said "gap"; following the write said otherwise.
- **False positive, worse.** Surrender *looked* covered because setting the MBID
  produced a record — while `purchase_unavailable`, the permanent half of the
  decision, moved silently. **A partial record hides a gap better than no record
  does.**

Trace what the operation actually writes, then check which fields of that write
are diffed. And fix gaps at the **diff**, not by adding calls at call sites: one
place covers every writer, including ones not yet written.

## 8. Audit load-bearing fields; document the exclusions

`_audit_sidecar_change` diffs identity, `purchase_unavailable` (a permanent
decision) and `track_count_expected` (it reclassifies the album). It deliberately
ignores `mb_match_candidate` (a suggestion rewritten on every Recheck — auditing
it would bury real changes in churn), the bookkeeping timestamps, and `notes`.

Write exclusions down in the docstring. An undocumented omission is
indistinguishable from an oversight, which is exactly how #88 was mis-filed.

## 9. Reading events back

Group in **one** query. The feed renders up to 100 entries and re-polls every
couple of seconds, so a per-entry lookup is an N+1 against a table that grows
without bound — `audit_by_action()` takes a list for this reason.

Any `<details>` in the feed needs `hmt-group` and a stable id, or the re-poll
slams it shut mid-read (see `web-ui`).

## Before you commit

1. Is this `activity` (user outcome) or `audit` (forensics)? Both?
2. Is every album id read **after** the mutation that might move it?
3. Is the operation inside an action scope — including new background work?
4. For a loop: is the intent recorded before the first write?
5. Are structured values in columns rather than duplicated into the message?
6. Did you check coverage by tracing the write, not by grepping the function?
