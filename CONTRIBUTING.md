# Contributing to Harmonist

Thanks for your interest in Harmonist. This is the short version of how work flows
through the project.

## Two tracks for every change

One question decides how a change is handled: **could it alter what Harmonist does
at runtime, or plausibly regress behavior?**

### Functional changes → issue, branch, PR

Bug fixes, new features, changes to existing behavior, and refactors that aren't
provably behavior-preserving take the full track:

1. **Open a GitHub issue first**, describing the problem or proposal. For a bug,
   include the symptom, the root cause once known, and the intended fix. Apply a
   type label — `bug` or `enhancement`.
2. **Work on a branch** whose name carries the issue number:
   `<type>/<N>-<slug>`, e.g. `fix/27-track-title`.
3. **Open a pull request** that references the issue (`Fixes #N`) so merging closes
   it. **CI must be green before it merges** — that's the gate.

### Non-functional changes → straight to `main`

Formatting, lint fixes, comment/docstring/typo edits, test-only additions, and
documentation don't need an issue, a branch, or a PR. Commit them directly to
`main`. (`no issue ⇒ no branch ⇒ no PR` — the three go together.)

If you can't confidently say a change is behavior-preserving, treat it as
functional.

## Before you commit

- **`make check`** must pass — it runs `ruff`, `ruff format --check`,
  `mypy --strict`, and the test suite. CI runs the same on Python 3.12–3.14.
- **CSS is a committed build artifact.** If you touch a template, run `make css`
  and commit the regenerated `static/harmonist.css`; CI fails on drift.
- **Changelog.** User-visible changes get a one-line entry under `[Unreleased]` in
  `CHANGELOG.md`, in the same commit.

## Where to read more

- **`README.md`** — what Harmonist is, how to run it, configuration, deployment.
- **`docs/design.md`** — the design spec: states, the sidecar schema, the tagging
  contract, and the matching/linking mechanics.
