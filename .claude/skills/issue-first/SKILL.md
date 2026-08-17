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

**Non-functional — no issue needed:**
- Formatting / `ruff` / `ruff format` / lint fixes.
- Comment, docstring, or typo edits with no code effect.
- Test-only additions that don't change `src/`.
- Documentation, and dependency bumps that CI handles and don't change behavior.

For functional work the tracks go together: **no issue ⇒ no branch; issue ⇒ branch
+ PR.** Never a half-measure (an issue but no branch, or a branch with no issue).

**Judgment call** — a "pure" internal refactor. If you can't state with confidence
that it's behavior-preserving, it's functional: it takes the full track. When still
unsure, ask the user rather than deciding unilaterally.

## Who may commit straight to `main`

The direct-to-`main` shortcut for non-functional work is a **maintainer** privilege
— `main` is protected and everyone else contributes via a PR, even for trivial
changes. Maintainers are whoever `.github/CODEOWNERS` lists (currently
`@randomphrase`).

Before committing anything directly to `main`, confirm you're acting as a
maintainer: `gh api user --jq .login` against the CODEOWNERS list, or
`gh api repos/randomphrase/harmonist/collaborators/<login>/permission --jq .permission`
(`admin`/`maintain` qualifies). **If you are not a maintainer, every change takes
the full PR track** — open an issue for functional work, or go straight to a
branch + PR for trivial work (no issue required).

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
7. **Land it locally, not with the merge button.** If `main` has moved, rebase
   onto it and force-push *first*, so CI validates exactly the commits that will
   land:

   ```
   git rebase origin/main <branch> --gpg-sign
   git push --force-with-lease origin <branch>     # CI runs; wait for green
   git checkout main && git merge --ff-only <branch>
   git push origin main && git push origin --delete <branch>
   ```

   GitHub's rebase-merge **re-creates the commits and strips their GPG signature**
   — the author survives, the signature doesn't. Landing it yourself keeps history
   linear *and* signed. The fast-forward makes the PR head reachable from `main`,
   so GitHub marks the PR merged, and `Fixes #N` in the commit closes the issue on
   push. This is not a CI bypass — the gate is that those exact commits went green.

**Don't stack PRs.** A PR whose base is another branch gets **closed** — not
retargeted — when that base is deleted on merge, and a closed PR can't be
reopened or retargeted. If work genuinely must be sequenced, land each piece to
`main` before opening the next PR.

Filing the issue and *then* immediately doing the work in the same session is fine
and expected — the point is the durable record and CI validation, not a waiting
period.

### Referencing an issue *without* closing it

Only use a closing keyword (`Fixes`/`Closes`/`Resolves #N`) when the merge should
close the issue. For interim work that references an issue it should **not** close
(one of several PRs, a partial step), use a neutral phrase: **`Workaround for #N`**,
`Part of #N`, `Re #N`, or `Toward #N`.

GitHub's parser **ignores negation** — writing "does not close #N" still matches
`close #N` and auto-closes the issue on merge. Never put a closing keyword
(`fix`/`close`/`resolve` + any suffix) next to an issue number unless you mean it,
even in a negated sentence. To say an issue **stays open**, phrase it *without* the
keyword at all:

| ✗ auto-closes on merge | ✓ leaves the issue open |
| --- | --- |
| `Does not close #33` | `Part of #33; stays open` |
| `Not resolved by this — see #33` | `Increment 1 of #33` |
| `Doesn't fix #14 yet` | `Toward #14` |

(This bit #42 once and #33 once — the rule is right, the negated phrasing is the
trap. If in doubt, drop the verb and just reference the number.)

## The trivial track (non-functional work)

**Maintainers** skip all of the above: no issue, no branch, no PR. Make the change
on `main`, run `review-gate` (it self-exempts docs/format-only diffs in one line),
and commit. Non-maintainers still branch + PR (see "Who may commit straight to
`main`" above). The `make check` quality gate applies to every commit either way.

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
where they fit (`idea`, `deferred`, `documentation`, `performance`, …); don't invent
new ones casually.

**Don't hard-wrap body prose.** GitHub renders issue, PR, and comment bodies as
Markdown and reflows paragraphs to the reader's width — wrapping prose at a fixed
column just bakes in awkward line breaks. Write each paragraph as one long line, with
a blank line between paragraphs; use normal Markdown for lists, headings, and code
fences. (This applies to `gh issue create`, `gh pr create`, and `gh … comment`
bodies — the wrapped examples below are illustrative, not a wrapping rule to copy.)

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

## Tracking issues: no hand-written checklists

An epic tracks its children through **GitHub's own sub-issue system**, not through a
`- [ ]` list in its body. Link a child with `gh issue edit <child> --parent <epic>`;
the epic then shows a live `sub-issues: 2/3` roll-up that updates itself when a child
closes.

A checklist in the body cannot do that. It goes stale the moment a child merges, and
the only way to keep it honest is to **edit the epic's body** — reaching into someone
else's issue to tick a box, which is off-putting to watchers and noisy in the
timeline. The roll-up is maintained by the same merge that closes the child, for free.

Work that isn't an issue yet still belongs in the body — an epic is where a direction
gets thought through, and filing a stub per idea is worse than describing it. Write it
as **prose or a plain bulleted list**, with no checkboxes: a bullet is a note, a
checkbox is a promise to come back and edit. Give an item its own issue when someone
picks it up, and the parent link puts it in the roll-up from then on.

## Why this exists

Commit messages scroll away and get squashed; issues are searchable, linkable, and
survive history rewrites. They're where dogfooding findings, root-cause notes, and
the rationale for a change accumulate — the record you'll want when a regression
shows up months later.
