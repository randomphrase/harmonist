---
name: changelog
description: Keep CHANGELOG.md current. When a change is user-visible — a new feature, a changed or removed behavior, a bug fix, or a UI/config/deployment change — add a one-line entry under the [Unreleased] section of CHANGELOG.md, in the same commit. Skip purely internal changes (refactors with no behavior change, tests, CI/tooling, dependency bumps, internal docs like design.md or AGENTS.md). Also covers rolling [Unreleased] into a dated version section when a release is tagged.
---

# Changelog maintenance

`CHANGELOG.md` is the user-facing record of what changed between releases — the
thing a user reads when deciding whether/what to upgrade. Keep it current *as you
work*, not retroactively at release time.

## When to add an entry

Add a one-liner under `## [Unreleased]` when the change is **user-visible**:

- A new feature or capability.
- A change to existing behavior a user would notice (a default, a flow, wording,
  a state name).
- A bug fix a user could have hit **on the latest release** — see below.
- A UI/UX change, a new or changed config option, or a change to how Harmonist is
  run/deployed.
- Anything security-relevant.

**Don't** add an entry for internal-only work: refactors with no behavior change,
test-only changes, CI/tooling, formatting, dependency bumps that don't change
behavior, or internal docs (`docs/design.md`, `AGENTS.md`, planning notes). The
test: *"would a user reading the release notes care?"* If no, skip it. If genuinely
unsure, ask.

### A bug that never shipped gets no entry

**A fix for a bug that only ever existed in unreleased code is internal work**,
however real the defect was. The broken version was never anybody's: an entry
for it describes a repair to something no user has seen, and asks them to care
about a week of this repo's internal history. The feature's own `### Added` /
`### Changed` entry is the whole story, and it is already true.

This is not a rare case. Building a feature — or reworking a representation, as
#468 did to artwork — turns up defects in the new code, each of which correctly
gets its own issue and its own fix. Every one of them is a bugfix commit, and
none of them belongs in the notes. 1.17.0 drafted twenty-three `### Fixed`
entries and shipped eight; the fifteen were bugs in controls that did not exist
in 1.16.1.

The issue says which — its **Since** line, per the `issue-first` skill. Where
it says both (new code repeating an old limitation on a new path), **the entry
covers only the released half**, which is usually narrower than the title: #489
was filed as "preserve additional pictures when replacing **or undoing**
artwork", but undoing an addition was new in that cycle, so the entry claims
only what replacement had always got wrong.

Decide it when you write the entry, not at release time — you know then. The
release audit is a backstop for what slipped through, not the place this gets
worked out from scratch.

## How to write it

- **One line, in plain user-facing language** — describe the *effect*, not the
  code. Good: "Sync options popover no longer closes when you move the cursor onto
  it." Bad: "add transparent hover bridge to #sync-control".
- **One sentence, and end with the issue number.** The entry answers *what
  changed for me?*; the issue answers *why, and how*. Anyone wanting the second
  is one click away, so don't pre-empt it here.

  ```
  ✓  Activity entries now link to their album (#65).
  ✗  Activity entries about a particular album now lead with that album's name,
     and the name is a link — click it and the album's detail opens over your
     Library. It's a normal URL (`/?album=<id>`), so you can bookmark or share it
     and it still works after a reload. The name is recorded with the entry, so
     older entries stay readable even after the album is renamed…
  ```

  That second one is real, from 1.1.0. It is accurate, and nobody will read it.
  **Rationale, mechanism and edge cases belong in the issue and the commit** —
  putting them here turns a scannable list into an essay and buries the entries
  either side of it.
- **Two lines is the ceiling**, and only when one genuinely won't carry the
  meaning. If you need a caveat, that's a signal the issue should carry it.
- Match the voice of the existing entries; keep it terse.
- Put it in the **same commit** as the change, at the top of `## [Unreleased]`.
- Once several entries accumulate, group them under `### Added`, `### Changed`,
  `### Fixed`, `### Removed`, or `### Security` (Keep a Changelog categories). A
  single stray entry doesn't need a group.

## On release

When cutting version `X.Y.Z`:

1. Rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD` (the release date).
2. Add a fresh, empty `## [Unreleased]` above it.

Do this in the same commit the release tag will point at, so the tagged commit's
`## [X.Y.Z]` section **is** the release notes (reuse it for the GitHub Release
body). Never invent entries at release time — they should already be there from
the work. The one exception is an entry that was *owed* and never written: the
`release` skill's step 1 audits `git log vPREV..main` for exactly that, and
recovering one there is a repair, not a retrofit.

Read the section before you cut, as the user will: a wall of prose is the moment
to tighten it, because after the tag the changelog and the published GitHub
Release have to be edited together to stay in step.

The rest of cutting a release — version bump, commit message, signed tag, the
GitHub Release, the workflows the tag fires — is the `release` skill.
