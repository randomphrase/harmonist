---
name: sidecar
description: How to change what a `.harmonist.json` sidecar contains. Consult BEFORE adding, renaming, retiring or repurposing any field in `models.Sidecar` / `sidecar.py`, and before touching `CURRENT_SCHEMA_VERSION`. Sidecars live in the user's music folders on their own hardware — there is no migration runner and no way to reach them — and the version check is a hard gate that makes every album vanish if you trip it. The rules that keep a field change survivable are collected here.
---

# Changing what a sidecar holds

`.harmonist.json` sits next to the user's audio files, on their disk, alongside
music they care about more than they care about Harmonist. It is the album's
identity and the record of every decision they made about it. There is no
migration runner, no staging environment, and no way to fix a bad change after
release — an upgraded Harmonist opens whatever files are already there.

Treat the sidecar as a **published interface with no version negotiation.**
That is not a metaphor: see rule 1.

Two documents own the content and must be updated with any change here —
`docs/design.md` §4 (the schema and what each field means) and the
`review-gate` skill's item 3 (state is derived, never stored). This skill is
the *mechanics*: what happens on disk when you change a field, and which
changes are survivable.

## 1. The schema version is a hard gate. Bumping it nukes every sidecar.

`_from_dict` checks **exact equality**, not a floor:

```python
sv = d.get("schema_version")
if sv != CURRENT_SCHEMA_VERSION:
    raise UnsupportedSchemaVersionError(
        f"sidecar at {source_path} has schema_version={sv}, expected "
        f"{CURRENT_SCHEMA_VERSION}. Delete the sidecar and re-reconcile."
    )
```

`scanner.resolve_album` catches that, logs a warning, and returns `None` — the
album is **not yielded at all**. So bumping `CURRENT_SCHEMA_VERSION` does not
migrate anything. It makes every existing album disappear from the Library and
the Inbox on the next scan, on every installation, until each user manually
re-reconciles a library that looks like it has been wiped.

**So: do not bump it.** It is `1`, and the goal is that it stays `1` forever.
A version bump is a last resort for a change that genuinely cannot be expressed
compatibly, and it needs the user's explicit sign-off plus a release note that
leads with it — not a line in the changelog.

Everything below exists so you never have to.

## 2. Unknown keys are ignored, which is what makes change cheap

`_from_dict` reads named keys with `d.get(...)` and never validates that the
dictionary contains nothing else. A sidecar carrying a key this build has never
heard of loads fine and the key is dropped on the next write.

That is the whole compatibility story, and it cuts both ways:

- **Adding** a field is backward-compatible in both directions. An older build
  ignores it; a newer build reads it as absent.
- **Retiring** a field is compatible too, *provided you stop reading it and
  stop writing it and change nothing else* (rule 4).
- **Renaming** a field is NOT one change, it is a retire plus an add, and doing
  it in one step silently loses every existing value. Don't.
- **Repurposing** a field — same name, different meaning or type — is the one
  genuinely dangerous edit, because old sidecars will parse and be *wrong*
  rather than absent. If you find yourself doing this, add a new field instead.

## 3. A new field must be load-bearing, and it must be absent by default

Before adding anything, the `review-gate` item 3 test: **is this derivable?**
State comes from the shape of the sidecar plus what is on disk. If a function
could compute it, it does not go in the file.

A field earns its place only if all of these hold:

- it records a **decision or an observation that cannot be re-derived** — a user
  intent (`purchase_unavailable`), or a fact about a moment that has passed;
- it has **at least one real reader** that changes what the user sees;
- it drives a **concrete affordance** in the UI. No speculative fields "for
  later", no audit-ish breadcrumbs — the audit log is over there.

`track_count_expected` is the cautionary tale (#195). It passes every test
above — it has a reader, it drives Complete vs Incomplete — and it is still
wrong, because the same number is already written into every audio file as
`Owned.TRACK_TOTAL` by the same tagging run, from the same MusicBrainz release.
"Load-bearing" is necessary, not sufficient. The other question is **is this
already recorded somewhere more authoritative?** For anything MusicBrainz told
us at tagging time, the answer is usually the tags.

Then, in `_to_dict`, **omit the default**:

```python
if s.purchase_unavailable:
    d["purchase_unavailable"] = True
```

Not `d["purchase_unavailable"] = s.purchase_unavailable`. Sidecars are read by
humans debugging their own libraries, and a file listing fifteen keys that are
all `null` or `false` hides the three that matter. The dataclass default is the
absent case; the file records only what departs from it.

## 4. Retiring a field: stop reading, stop writing, leave the key alone

The deprecation path, in order:

1. **Stop reading it.** Remove the consumer and whatever it drove. The field is
   now dead weight, but nothing has changed on disk.
2. **Remove it from `_from_dict`.** Old sidecars still carry the key; it is now
   one of the unknown keys rule 2 ignores.
3. **Remove it from `_to_dict`.** New writes stop emitting it. This is the step
   that actually removes it from users' disks — but only from albums that get
   rewritten for some other reason.
4. **Remove it from `models.Sidecar`,** and from the load-bearing list in
   `_audit_sidecar_change` if it was there (rule 5).
5. **Update `docs/design.md` §4** — the schema block and the prose. A field
   documented but not implemented is worse than one that is neither.

Do **not** bump `CURRENT_SCHEMA_VERSION` (rule 1). Do **not** write a pass that
rewrites every sidecar to strip the key: it is a library-wide write to user data
to remove something already being ignored, and the risk is entirely on the wrong
side. Stale keys are harmless and will age out.

Two things to check before you start:

- **Is anything else the only writer of it?** Retiring a field usually means a
  code path exists only to populate it, and that path goes too.
- **Does a user-visible number change?** If the field fed a badge or a filter
  count, the deprecation is user-visible and needs a `CHANGELOG.md` entry, even
  though the file format "did not change".

## 5. Load-bearing fields are audited; the list is hand-maintained

`_audit_sidecar_change` writes a `sidecar.update` row when a **load-bearing**
field moves, and stays silent otherwise. The list is explicit, in
`sidecar.py`:

- identity — `mb_release_id` / `bandcamp.item_id` / `store_url`
- `purchase_unavailable` — a surrender, effectively permanent
- `track_count_expected` — reclassifies Complete ↔ Incomplete

and deliberately *not* `mb_match_candidate` (a suggestion, rewritten on every
Recheck — auditing it would bury real changes in churn), the timestamps
(audited by the events that set them), or `notes` (free text, no derived
consequence).

**Adding a field means deciding which side of that line it falls on, and
editing the diff block by hand.** Nothing enforces it. Ask: if this value
changed behind the user's back, would they need to be able to reconstruct what
it was? If yes, it is audited. See the `event-recording` skill for the traps
around getting the record itself right.

## 6. Never hand-roll the write — or the rebuild

`sidecar.write()` is the only writer, and it does five things besides
serialising JSON:

- reads the previous sidecar for the audit diff;
- **normalises identity** — enforces that exactly one of
  `(mb_release_id, temp_uid)` is non-null, asserted, not hoped;
- writes temp → `fsync` → `os.replace`, so a crash mid-write leaves the old
  file intact rather than a truncated one;
- records the identity alias when the album's id moves, which is knowable only
  at that instant;
- updates `library_index`, the in-memory dedup index.

Constructing the dict and calling `json.dump` yourself skips all five. If you
need to change one field, read, `dataclasses.replace`, write.

**Re-read before you write.** A `Sidecar` from a scan snapshot can be minutes
old on a large library; writing it back clobbers anything that landed in
between. `reconcile.backfill_track_count` re-reads and re-checks its
precondition for exactly this reason.

### Rebuild with `replace`, never by naming the fields to keep

`sidecar.write()` being the only writer is not enough on its own, because it
writes whatever it is handed. The other half of the rule is how you build that
object:

```python
new = replace(sc, mb_release_id=None, mb_match_candidate=candidate)   # yes
new = Sidecar(store_url=sc.store_url, mb_release_id=None, ...)        # no
```

A fresh `Sidecar(...)` listing what to carry forward silently resets **every
field you didn't name** to its default. That is not a hypothetical: #239 found
it in `_tag_with_release` and fixed that one site; #263 found the same
construction at **twelve** more, all of them dropping `video_media` and
`tracks_unavailable`, eleven dropping `purchase_unavailable`. The surrenders are
the worst case — they are decisions with no evidence on disk, so once defaulted
they cannot be recovered by anything, and the album re-surrenders or re-accuses
itself of missing tracks on the next pass.

`replace()` carries unnamed fields **by construction**, so a field added to the
model tomorrow needs no edit at any call site and cannot be dropped. That
property is what makes rule 3's "add a field" safe at all.

`test_sidecar_carries.py` enforces this by parsing `src/` for `Sidecar(...)`
constructions that mention an existing sidecar. If your new code trips it, use
`replace`; do not add yourself to the exemption set.

**The one exemption is `scanner._merge_sidecars`,** which builds an album's view
of its folders' sidecars (#197) and genuinely needs a rule per field — first /
earliest / latest / any — which `replace()` off some arbitrary part cannot
express. It pays for that by being held **total**: a second test asserts it names
every field on the model, including the ones it deliberately drops (`temp_uid`).
**Adding a field to `models.Sidecar` means adding a merge rule there**, and that
test will tell you so.

## 7. `delete_all` is the escape hatch, and it is the user's to pull

Nuke (`sidecar.delete_all`) removes every sidecar and lets the library rebuild
from the file tags. It exists so a bad state is recoverable without anyone
hand-editing JSON, and it is one of the reasons a schema change does not *have*
to be perfect.

That is not a licence to lean on it. Nuke discards everything the tags cannot
re-derive — Bandcamp item ids, surrenders, notes, the `added_at` history — so a
change whose remediation is "the user nukes" is a change that loses data the
user chose. Offer it; never assume it.

## Checklist

Before committing a sidecar content change:

- [ ] `CURRENT_SCHEMA_VERSION` untouched (rule 1)
- [ ] New field is not derivable, and is not already in the tags (rule 3)
- [ ] New field has a reader and a visible affordance (rule 3)
- [ ] `_to_dict` omits the default (rule 3)
- [ ] `_audit_sidecar_change`'s load-bearing list considered explicitly (rule 5)
- [ ] Every rewrite uses `replace`, not a fresh `Sidecar(...)` (rule 6)
- [ ] `scanner._merge_sidecars` gives the new field a merge rule (rule 6)
- [ ] `docs/design.md` §4 updated — schema block *and* prose
- [ ] Round-trip test: write → read → identical `Sidecar`
- [ ] Old-sidecar test: a dict without the new key loads at the default
- [ ] For a retirement: a dict *with* the stale key still loads (rule 2)
- [ ] `CHANGELOG.md` entry if any user-visible number or badge changes
- [ ] `review-gate` run, as for every commit
