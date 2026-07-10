---
name: changelog
description: Keep CHANGELOG.md current. When a change is user-visible — a new feature, a changed or removed behavior, a bug fix, or a UI/config/deployment change — add a one-line entry under the [Unreleased] section of CHANGELOG.md, in the same commit. Skip purely internal changes (refactors with no behavior change, tests, CI/tooling, dependency bumps, internal docs like design.md or CLAUDE.md). Also covers rolling [Unreleased] into a dated version section when a release is tagged.
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
- A bug fix a user could have hit.
- A UI/UX change, a new or changed config option, or a change to how Harmonist is
  run/deployed.
- Anything security-relevant.

**Don't** add an entry for internal-only work: refactors with no behavior change,
test-only changes, CI/tooling, formatting, dependency bumps that don't change
behavior, or internal docs (`docs/design.md`, `CLAUDE.md`, planning notes). The
test: *"would a user reading the release notes care?"* If no, skip it. If genuinely
unsure, ask.

## How to write it

- **One line, in plain user-facing language** — describe the *effect*, not the
  code. Good: "Sync options popover no longer closes when you move the cursor onto
  it." Bad: "add transparent hover bridge to #sync-control".
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
the work.
