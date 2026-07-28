---
name: issue-first
description: Work-tracking gate for Harmonist. Consult at the START of any change. Substantial or functional work — bug fixes, features, behavior/UX changes, and refactors that aren't provably behavior-preserving — must be raised as a GitHub issue BEFORE the code is written, done on an issue-numbered branch, and merged via a PR once CI is green; the merge closes the issue. Only changes that cannot alter runtime behavior (formatting, lint fixes, comments/docstrings, typos, test-only additions) may skip all of that and commit straight to main. When unsure which side of the line a change falls on, treat it as functional.
---

# Issue-first: raise it before you fix it

Now that Harmonist is released, work needs a paper trail. Every substantial change
starts as a GitHub issue on `randomphrase/harmonist`, so there's a durable record
of *what was wrong or wanted*, *why*, and *how it was addressed* — independent of
the commit message. This skill gates the **start** of work; `review-gate` gates the
**commit**.

## The two tracks

One question decides everything: *could this alter what Harmonist does at runtime,
or plausibly regress behavior?* The answer routes the work down one of two tracks —
there is no third.

**Functional — the full track (issue → branch → PR → CI → merge):**
- Bug fixes (any change to behavior a user could have hit).
- New features or capabilities.
- Changes to existing behavior a user would notice: a default, a flow, wording, a
  state name, a config option, deployment.
- Refactors you cannot confidently call behavior-preserving.

**Non-functional — straight to `main`, no issue, no branch, no PR:**
- Formatting / `ruff` / `ruff format` / lint fixes.
- Comment, docstring, or typo edits with no code effect.
- Test-only additions that don't change `src/`.
- Dependency bumps that CI handles and that don't change behavior.

These three go together: **no issue ⇒ no branch ⇒ no PR.** If a change doesn't
warrant an issue, it doesn't warrant a branch either — commit it directly to
`main`. If it *does* warrant an issue, it takes the whole track, branch and PR
included. Never a half-measure (an issue but a direct commit, or a branch with no
issue).

**Judgment call** — a "pure" internal refactor. If you can't state with confidence
that it's behavior-preserving, it's functional: it takes the full track. When still
unsure, ask the user rather than deciding unilaterally.

## The full track (functional work)

1. **Confirm the problem.** For a bug, reproduce it (demo mode is the fast path)
   or pin down the root cause in code before filing — a good issue names the cause,
   not just the symptom.
2. **Search first.** `gh issue list --search "<keywords>"` — the backlog already
   lives in issues. Extend or comment on an existing one rather than duplicating.
3. **File the issue** with `gh issue create` (see below). Get its number.
4. **Branch off `main`.** The **branch name must carry the issue number** so the
   work is traceable from git alone — `<type>/<N>-<slug>`, e.g. `fix/27-track-title`,
   `feat/31-genre-tags`. Never commit functional work directly to `main`.
5. **Do the work and commit.** Put `Fixes #N` (or `Closes #N`) in the commit body
   so the merge closes the issue. Run `review-gate` before committing, and add a
   `CHANGELOG.md` entry if the change is user-visible (`changelog` skill).
6. **Open a PR and let CI validate.** `gh pr create` targeting `main`, body ending
   with `Fixes #N`. **CI green is the merge gate** — `make check` passing locally is
   necessary but not sufficient; the PR must go green before it lands. Don't merge a
   red or pending PR, and don't bypass CI with a direct push to `main`.

Filing the issue and *then* immediately doing the work in the same session is fine
and expected — the point is the durable record and CI validation, not a waiting
period.

## The trivial track (non-functional work)

Skip all of the above: no issue, no branch, no PR. Make the change on `main`, run
`review-gate` (it self-exempts docs/format-only diffs in one line), and commit. The
`make check` quality gate still applies to every commit.

## What a good issue contains

For a **bug**:
- **Symptom** — what the user observed (their words / a concrete repro).
- **Root cause** — the actual defect in code, once known (`file:line`).
- **Fix approach** — one or two lines on the intended change.
- Reference material if any (a MusicBrainz URL, a log excerpt).

For a **feature / change**: the motivation and the desired behavior. Keep it terse;
link related issues.

**Always apply a type label** so the issue is triageable: `bug` for a defect,
`enhancement` for a feature or change. Add situational labels already in the repo
where they fit (`idea`, `deferred`, `documentation`, …); don't invent new ones
casually.

```
gh issue create --repo randomphrase/harmonist \
  --title "<concise imperative summary>" \
  --label bug \
  --body "$(cat <<'EOF'
**Symptom**
...

**Root cause**
...

**Fix**
...
EOF
)"
```

## Why this exists

Commit messages scroll away and get squashed; issues are searchable, linkable, and
survive history rewrites. They're where dogfooding findings, root-cause notes, and
the rationale for a change accumulate — the record you'll want when a regression
shows up months later.
