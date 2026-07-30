---
name: bulk-refactor
description: How to perform mechanical edits across many call sites at once — renaming an API, changing a signature, converting literals to an enum, adding a keyword argument everywhere. Consult BEFORE writing any script, sed command, or regex that will rewrite more than a handful of Python call sites. A bulk edit that produces valid, tested, plausible-looking code while silently changing behavior is the failure mode this skill exists to prevent — it has already happened here once.
---

# Bulk refactors

The danger of a mechanical edit across dozens of call sites is not that it breaks
the build. It's that it **doesn't**. A regex that mangles four calls out of thirty
leaves valid Python, a green test suite, and a diff too large to read line by
line. The wrongness ships.

This happened in #47 (the `Level` StrEnum conversion): a `re.DOTALL` pattern
matched *across* call boundaries and swapped the severity of four events —
`"Reconcile started…"` info→warning, `"adopted external re-tag"` warning→info,
and two more. All 623 tests passed. It was caught by the user reading the diff,
not by any tool.

## 1. Never rewrite code with a multi-line regex

A regex has no concept of where a call expression ends. `\(.*?\)` with `DOTALL`
will happily span from one call's opening paren to a *later* call's closing paren
whenever the intervening text doesn't match your assumptions.

- Single-line, anchored, mechanical substitutions (an import line, a bare rename
  with no arguments involved) are fine.
- Anything that has to understand **call structure** — arguments, keywords,
  nesting — must not be done with a regex. Use `ast`.

If you catch yourself adding `re.DOTALL` to a rewrite pattern, stop. That flag is
the tell.

## 2. Use `ast` to find the sites, and edit in byte space

Parse with `ast`, walk to the nodes you want, and use `node.lineno` /
`node.col_offset` / `node.end_col_offset` to locate the exact span.

Two traps that have both bitten here:

- **`col_offset` is a UTF-8 *byte* offset, not a character offset.** Any source
  line containing an em-dash (this codebase is full of them) shifts every
  subsequent column. Convert the line to `bytes`, splice there, and decode back —
  or your insert lands mid-word. Assert the byte you expect (`src[close] == ")"`)
  *before* writing anything.
- **`ast.walk` on an outer function also yields the nodes of every nested
  function.** Walking `create_app` and then separately walking each inner function
  annotates every inner call site twice — producing duplicate keyword arguments
  and a `SyntaxError`. Use a `NodeVisitor` that tracks the innermost enclosing
  function, plus a dedupe set keyed on position.

Edit from the **end of the file backwards**, so earlier offsets stay valid.

## 3. Back up, then verify semantic equivalence against HEAD

Before running the rewrite: `cp target.py target.py.bak`. When a run goes wrong,
restore from the backup and redo — don't try to repair a half-applied rewrite by
hand.

After running it, **prove the change did only what you intended.** A diff review
is not enough at this scale, and neither is a green suite. Write a throwaway
audit script that parses both HEAD and the working tree with `ast` and compares
the property you were *not* meant to change:

```python
# for each matching call, extract (message, level) from HEAD and from the worktree
# and assert the pairs are identical modulo the intended transformation
```

For #47 that meant: same message → same severity, for all 32 sites. The audit
printed `✓ all activity levels preserved vs HEAD` — and only then was the change
trustworthy. Run the audit, paste its result, and delete the script.

State the invariant the audit checks in one line before you write it. If you
can't state it, you don't yet know what the refactor is allowed to change.

## 4. Prefer letting the type checker do the work

Often the safest bulk refactor is the one you don't script. Changing a
parameter's type so `mypy --strict` flags every incompatible call site turns a
silent rewrite into an enumerated worklist you fix deliberately. Slower per site,
but the failure mode is a type error, not a behavior change.

`Source`/`Level` became `StrEnum` precisely because a `StrEnum` compares equal to
its string value — no data migration, existing Jinja comparisons still work — so
the blast radius was containable in the first place. Look for that kind of
representation choice before reaching for a script.

## 5. Scope and commit

- One refactor per commit, with **nothing else in it**. A behavior change hidden
  inside a 32-file mechanical diff is invisible.
- Mechanical refactors that are *provably* behavior-preserving can go straight to
  `main`; anything that could alter runtime behavior needs an issue and a PR (see
  the `issue-first` skill). A bulk edit you had to audit for semantic drift is, by
  definition, in the second category.
- Say in the commit message how the equivalence was verified.
