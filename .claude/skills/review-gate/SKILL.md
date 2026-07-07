---
name: review-gate
description: Pre-commit design-invariant review for Harmonist. MUST be consulted before every commit, and before declaring any implementation task complete. Also consult whenever a change touches sidecars, tag writing, matching/lookup logic, MusicBrainz calls, state transitions, destructive operations, or adds any new field, flag, or state. Tests and mypy verify correctness; this gate verifies the design invariants they cannot see. Do not skip it because the diff "seems small" — small diffs are where invariants erode.
---

# Review Gate: Harmonist Design Invariants

Run through this checklist against the full diff about to be committed. Each item
is an invariant from `docs/design.md` that a plausible-looking change can violate
without any test failing. For each item, answer: **does this diff touch the area?
If yes, does it hold the invariant?** If an invariant is violated, stop and fix it
(or raise it with the user) before committing — do not note it as a TODO.

If the diff genuinely touches none of these areas (e.g. docs-only, CSS-only),
say so explicitly in one line and proceed.

## 1. Audit coverage

Every operation that destroys or replaces information must write a record to the
audit log before/as it acts: downloads, file moves or overwrites, sidecar
rewrites, demotions, checkpoint clears, surrenders.

- Does the diff add or modify any such operation?
- If yes: is there a corresponding audit write, with enough detail to reconstruct
  what happened and reverse it manually?
- New destructive operation types need a new audit event type, not a reused one.

## 2. No guessing, no scraping

Identity comes from authoritative sources only. Never fabricate URLs or IDs
(e.g. constructing an `/album/` slug from a title). Never scrape pages for
metadata. Matching logic must be **exact, scoped, and unique**:

- *Exact*: normalized string equality, never fuzzy/similarity scoring.
- *Scoped*: matches only searched within an already-confirmed context
  (e.g. within one artist's releases), never globally.
- *Unique*: a match that isn't unambiguous is no match; ambiguity goes to the
  review inbox, it is never auto-resolved.

Any relaxation of these three properties is a design change requiring explicit
user sign-off, not a code review comment.

## 3. State is derived, never stored

An item's state is computed from the shape of its sidecar plus what exists on
disk. There is no state field, no `incomplete` flag, no `needs_review` boolean.

- Does the diff add any field to a sidecar (or elsewhere) that records a status
  a function could instead derive?
- Every new persisted field must be **load-bearing**: it must have at least one
  reader, and it must drive a concrete affordance in the UI or CLI. No
  speculative fields "for later".
- Sidecar writes remain atomic (write temp file, rename). No partial writes.

## 4. Non-destructive to user data

Harmonist never destroys information the user (or Bandcamp) put in their files.

- Do any tag-writing paths in the diff strip, overwrite, or fail to round-trip
  the comment field (`©cmt` / `COMM`) or any user-set tag not owned by Harmonist?
- Surrender must only rewrite the sidecar; it never modifies on-disk tags.
- No code path renames, moves, or reshuffles directories the user organized,
  unless the user explicitly initiated that exact move and it is audited (see 1).

## 5. Escape hatch for every state

The user must never need to hand-edit a `.harmonist.json` to get out of a state.

- Does the diff introduce any new state, condition, flag, or failure mode?
- If yes: is there a path out via the UI, via Picard/on-disk convention, or via
  an existing command? "The user can edit the JSON" is a design bug.
- Dead ends discovered during implementation go to the user as a design
  question, not silently papered over.

## 6. MusicBrainz call budget

MB access is rate-limited (1 request/second) and bounded.

- Do any new or moved MB calls go through the shared rate-limited client? A
  direct call that bypasses it is a violation even if it "only runs once".
- Is the number of MB calls per user action bounded by a constant or by the
  size of the user's explicit selection — never by library size in a loop the
  user didn't ask for?

## 7. Idempotent transitions

Running sync, recheck, or tag twice must produce the same result as running it
once. No duplicate downloads, duplicate audit entries for a no-op, or oscillating
sidecar rewrites.

- Does the diff add or change a transition? If yes, point to (or add) the test
  that runs it twice and asserts a no-op the second time. This is the one item
  where a test IS the gate — require it.

## Output format

After checking, emit a short block in the commit conversation (not the commit
message):

```
Review gate: [pass | N issues]
Touched areas: audit, sidecar-fields   (or "none")
Notes: <one line per non-trivial judgment call>
```

Judgment calls that were close (e.g. "this field is arguably derivable") must be
surfaced to the user, not decided unilaterally.
