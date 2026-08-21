---
name: source-control
description: Who authorizes a git or gh operation, and how history is kept clean. Consult BEFORE any `git push`, `gh pr create/merge`, `gh release create/edit`, or anything else that leaves this machine; before staging (`git add`); and before any history rewrite (rebase, amend, force-push) of commits that are already public. Publishing is the user's call every time — a skill listing `git push` among its steps is documentation, not permission.
---

# Source control

The rules about *what leaves the machine and when*. Workflow skills — `release`,
`issue-first` — own the shape of their flows; this owns the authorization and the
hygiene, so it isn't restated in three places and enforced in none.

## 1. Never push or publish without an explicit say-so

**Commit freely. Publishing is the user's call, every time — ask, and wait.**

Applies to everything that leaves the machine: `git push` (branch or `main`),
`gh pr create`, `gh pr merge`, `gh release create`, `gh release edit`, `git push
--tags`, and any workflow those trigger.

None of the following is authorization. Each has been used as one:

- **A skill lists `git push` among its steps.** `release` step 7 and
  `issue-first` step 7 spell out *what* the push is and *what order* it goes in.
  That is documentation. It never means you may run it unasked.
- **The user's request implies publishing.** "Cut 1.10.0" means *prepare* the
  release. Preparing and publishing are two approvals, and the gap between them
  is the point — it's where they read what you wrote.
- **Stopping would leave a flow half-finished.** Being mid-flow is not consent.
  Stop mid-flow and ask; an unpushed commit and a verified tag are a perfectly
  good place to stand.
- **CI is green.** That's a quality gate, not a permission gate.
- **They approved a push earlier in the session.** Approval doesn't carry
  forward. One go-ahead covers the operation asked about and nothing beyond it —
  a follow-up fix an hour later is a fresh ask.

1.10.0 was pushed, tagged and published to GHCR without asking, on the second
and third reasons above. Nothing broke, which is exactly why it's written down:
the rule can't rely on the failure being loud.

**How to hand off.** Say what's committed and that it's held locally, then stop.
Don't report how many commits are ahead of `origin`, don't estimate it, and don't
nudge — the count is noise and it's stale the moment they act on it.

## 2. Stage explicit paths

`git add CHANGELOG.md pyproject.toml` — never `git add -A`, `git add .`, or
`git commit -a`. A blanket stage once swept in files that had to be scrubbed from
public history afterwards. Name what you mean; if the list is long, that's a
signal the commit is doing too much.

## 3. Don't rewrite published history

Once commits are on `origin`, fix forward: an ordinary mistake gets a follow-up
commit, not a rebase. `git rebase`, `git commit --amend` and `--force-with-lease`
are for work that is still local, or for a genuine emergency — a leaked
credential, something that must not stay in the log — and an emergency rewrite is
itself a thing to ask about first.

Rebasing an *unpushed* branch onto `main` before it lands is normal and expected;
that's `issue-first` step 7, and it is not what this rule is about.

## 4. Sign what you land

Tags are signed (`git tag -s`) and verified locally *before* they go anywhere.
Merges are fast-forward, landed from the terminal — GitHub's merge button
re-creates commits and strips their GPG signatures. The mechanics live in
`issue-first` step 7 and `release` step 7.

## Done when

- [ ] nothing reached a remote without the user saying so, in this exchange
- [ ] every `git add` named its paths
- [ ] no published commit was rewritten
- [ ] no ahead-of-origin count and no push nudge in what you told them
