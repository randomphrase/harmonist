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
- **"Elsewhere" includes `activity.db`** — a new column or table there is
  persistence with the same rule, and an append-only migration makes a
  speculative one permanent. See the `schema-migration` skill.
- **The load-bearing rule is not only about fields.** A new *function* with no
  production caller is the same defect wearing different clothes: it reads as
  supported API, it accretes tests that pin behaviour nothing depends on, and
  the next person extends it rather than deleting it. Grep for a caller. If the
  only hits are the tests you just wrote, delete it and leave a comment saying
  why it does not exist — the reasoning is what stops it coming back. (#127's
  `forget_release` reached the gate this way, and its stated purpose turned out
  to be impossible anyway.)
- **If a fact is derived from two sources, can the two disagree?** State is
  derived, which is only safe while the inputs agree. When one writer takes a
  value from a request and another takes it from the response, a redirect makes
  them differ and the album derives a state neither writer intended — #268
  exactly, where the sidecar kept the requested MBID while the files got the
  one MusicBrainz actually returned. Prefer rebinding to the authoritative value
  once, at the seam, so agreement holds by construction rather than by two call
  sites remembering.
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
- **Staleness is one of these conditions**, and an easy one to miss because
  nothing looks broken. Anything cached or served from a stored answer puts the
  user in a state — "what I am looking at may be out of date" — that they can
  neither see nor leave unless you build both halves: say *how old* the answer
  is, and give them a control that gets a newer one. #127's "read 20 minutes
  ago / read again" is the shape. A config knob alone is not an escape hatch.
- Dead ends discovered during implementation go to the user as a design
  question, not silently papered over.

## 6. MusicBrainz call budget

MB access is rate-limited (1 request/second) and bounded.

- Do any new or moved MB calls go through the shared rate-limited client? A
  direct call that bypasses it is a violation even if it "only runs once".
- **Does every by-id fetch go through `mb_cache` rather than `mb_lookup`
  directly?** Since #127 the cache is where the budget is actually spent or
  saved, and a direct `mb_lookup.fetch_release` is now a *silent* regression:
  it works perfectly, returns the right data, and quietly costs a request that
  did not need spending. Nothing fails, so nothing catches it but this question.
- A caller that legitimately must not be served a stored answer — it writes
  tags, or the user pressed it to force a re-check — passes
  `max_age=mb_cache.FRESH`; it does **not** reach round the cache to
  `mb_lookup`. Going through keeps the row (and so #32's change-detection
  baseline) refreshed by the fetch that was happening anyway.
- Count the requests in a test rather than reasoning about them. The request
  count *is* the feature, and it is the one thing an otherwise-correct cache
  can get wrong without any assertion noticing.
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
make check: exit 0                     (the exit code, not a line of its output)
Notes: <one line per non-trivial judgment call>
```

Judgment calls that were close (e.g. "this field is arguably derivable") must be
surfaced to the user, not decided unilaterally.

**Report `make check` by its exit code.** It runs five steps in order and prints
no summary of its own, so the cheerful `All checks passed!` is **`ruff check`'s**
line — step 1 of 5. Grepping the output for it reports success while
`format-check` is failing and make has already stopped, meaning mypy,
template-lint and pytest never ran. Run
`make check > /tmp/check.log 2>&1; echo $?` and quote the number. (Reading
pytest's own `N passed` tail is also sound, since the tests are the last step
and only run if everything before them passed.) This gate exists to make claims
about a diff; a claim about the quality gate has to be one you actually
checked.
