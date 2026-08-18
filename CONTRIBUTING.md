# Contributing to Harmonist

Thanks for your interest in Harmonist. This is the short version of how work flows
through the project.

## Every change comes as a pull request

All contributions land through a PR — there is no direct pushing to `main`. Fork
the repo (or branch, if you have write access), do the work on a branch, and open a
pull request. **CI must be green before a PR merges** — that's the gate.

### Functional changes need an issue first

Bug fixes, new features, changes to existing behavior, and refactors that aren't
provably behavior-preserving should start with a GitHub issue, so the *what* and
*why* are recorded before the code:

1. **Open an issue** describing the problem or proposal. For a bug, include the
   symptom, the root cause once known, and the intended fix. Apply a type label —
   `bug` or `enhancement`.
2. **Branch** with the issue number in the name: `<type>/<N>-<slug>`, e.g.
   `fix/27-track-title`.
3. **Open the PR** referencing the issue (`Fixes #N`) so merging closes it.

If you can't confidently say a change is behavior-preserving, treat it as
functional and open an issue.

### Trivial changes still come as a PR

Formatting, lint fixes, comment/docstring/typo edits, test-only additions, and
documentation don't need an issue — but they still go through a branch and a PR so
CI can validate them. When in doubt, just open the PR; a maintainer will help.

## Before you commit

- **`make check`** must pass — it runs `ruff`, `ruff format --check`,
  `mypy --strict`, and the test suite. CI runs the same on Python 3.12–3.14.
- **CSS is a committed build artifact.** If you touch a template, run `make css`
  and commit the regenerated `static/harmonist.css`; CI fails on drift.
- **Changelog.** User-visible changes get a one-line entry under `[Unreleased]` in
  `CHANGELOG.md`, in the same commit.

## Where to read more

- **`README.md`** — what Harmonist is and where it fits.
- **`docs/usage.md`** — the user guide; **`docs/installation.md`** for running and
  configuring it, **`docs/deployment.md`** for exposing it safely.
- **`docs/design.md`** — the design spec: states, the sidecar schema, the tagging
  contract, and the matching/linking mechanics.
