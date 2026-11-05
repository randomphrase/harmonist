# Contributing to Harmonist

Thanks for your interest in Harmonist. This is the short version of how work flows
through the project.

## Ways to help

Code is only one of them:

- **Use it on your own library and report what you find.** A large or long-lived
  collection turns up problems a small one never does.
- **Review the docs** with fresh eyes, especially
  [getting started](docs/getting-started.md). A newcomer spots gaps the authors
  can't.
- **Polish the UI.** Harmonist should be a joy to use. Anything that gets in the
  way of that, however small, is worth an issue.
- **Spread the word** to people who'd find Harmonist useful.
- **Improve MusicBrainz.** A correction made there reaches your files through
  Harmonist, and everyone else's too.
- **Write code.** Before starting on an issue, say so on it, so that two people
  don't end up working on the same thing. For work with no issue yet, open one
  first (see below).

## Development workspace

For a fresh clone, see [the devcontainer setup](docs/development.md).
Outside the devcontainer, run `make browser-assets` with Node 24+ and npm before
starting the app or browser tests. Repeat after changes to `package-lock.json`.

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
  `mypy --strict`, and the test suite. CI runs the same on Python 3.14 and 3.15.
- **CSS is a committed build artifact.** If you touch a template, run `make css`
  and commit the regenerated `static/harmonist.css`; CI fails on drift.
- **Changelog.** User-visible changes get a one-line entry under `[Unreleased]` in
  `CHANGELOG.md`, in the same commit.

## Where to read more

- **`README.md`** — what Harmonist is and where it fits.
- **`docs/getting-started.md`**, **`docs/how-it-works.md`**,
  **`docs/troubleshooting.md`**, **`docs/configuration.md`** and
  **`docs/security.md`** — the user docs.
- **`docs/design/`** — the design: why Harmonist is built the way it is.
