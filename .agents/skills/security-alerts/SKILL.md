---
name: security-alerts
description: How to triage a GitHub code scanning (CodeQL) alert on randomphrase/harmonist. Consult BEFORE acting on any alert — including one the user pastes or names by number, and before "fixing" anything to make an alert go away. Most alerts raised here are re-flags of decisions issue #63 already settled, and the mechanics that make them reappear (plus the 280-char dismissal limit and the fact that suppression is unavailable on default setup) are collected here so each round costs a minute instead of an afternoon.
---

# Security alerts: triage before you fix

Harmonist runs CodeQL **default setup**. Most alerts that land here are not new
findings — they are the same two accepted-risk families reappearing under new
numbers. Four of the five alerts handled in August 2026 were re-flags of
decisions [#63](https://github.com/randomphrase/harmonist/issues/63) had already
made. Treating each one as a fresh security problem is how you end up rewriting
working code to satisfy a scanner.

So the first question is never *"how do I fix this?"* It is *"has this already
been decided?"*

## 1. Triage: is this new, or a re-flag?

Before reading the flagged code, check whether its rule has been ruled on:

```
gh api repos/randomphrase/harmonist/code-scanning/alerts \
  --jq '.[] | "\(.number) \(.state) \(.rule.id) \(.most_recent_instance.location.path):\(.most_recent_instance.location.start_line)"'
```

A dismissed alert with the **same rule id in the same file** is your answer —
read its `dismissed_comment`, which points at the issue that settled it. #63 is
the standing record for both families below; its comment thread carries the
triage notes.

## 2. The two standing decisions

**`py/stack-trace-exposure` in `src/harmonist/web/main.py` → `won't fix`.**
These are exception *messages* (`MBError`), not tracebacks. Harmonist is
single-user and self-hosted behind Basic auth: the only reader is the operator,
and the upstream MusicBrainz error text is the diagnostic they actually need when
a lookup fails. #63 weighed suppressing it and chose to keep it.

**`py/incomplete-url-substring-sanitization` in `test/` → `used in tests`.**
Assertions of the form `assert "bandcamp.com" in r.text` over a response body,
log line, or tag value. Not sanitization. Real host checks go through
`models.host_is()` (`src/harmonist/models.py:256`), which #63 added to fix the
genuine instances in `src/`. Contorting an assertion to satisfy the scanner makes
the test worse — don't.

An alert of either shape gets dismissed, not fixed. Anything else is genuinely
new: read the code, and if it's a real defect take the full `issue-first` track.

## 3. What actually makes an alert come back

This is the part that cost the time. **CodeQL alert tracking survives a pure line
move.** Alert #12 followed `_flash` from `web/main.py:3217` to `:3335` across the
1.6.0 release and kept its dismissal untouched. What breaks the fingerprint is
editing the **content of the flagged line** — #142 rewrote the compare handler's
error fragment, which retired #13 and opened #14 on the same code, for the same
reason, already dismissed twice.

Two consequences:

- Don't predict that an alert "will keep coming back as code shifts". It won't.
  Re-flags are rare and specific.
- **Editing a line that carries a dismissed alert costs you a re-dismissal.**
  Expect it, and don't read the new number as a new problem.

## 4. Separate the alert from the defect

They are different questions and deserve separate answers:

- *Is the flagged code wrong?* Decide on the merits, ignoring the scanner.
- *Should the alert close?* Decide from §2.

#142 is the worked example. The `py/stack-trace-exposure` alert was accepted
risk — but sitting underneath it was a real defect the scanner wasn't reporting:
the exception text went into an HTML f-string **unescaped**. Worth fixing on its
own merits; it did not and could not clear the alert, because CodeQL tracks
exception text reaching a response regardless of escaping.

Say so explicitly when you fix code near a dismissed alert, in both the commit
message and the dismissal comment, so nobody later reads the fix as a failed
attempt to satisfy CodeQL.

(A sanitizer *can* retire an alert: `html.escape()` cleared the taint at the
`_flash` site for good. Just don't count on it.)

## 5. Suppression is not available — stop looking

Already researched, twice. Don't spend the round-trips again:

- **Query filters** (`.github/codeql/codeql-config.yml`, `query-filters: exclude`)
  require **advanced** setup. This repo has no CodeQL workflow — `analysis_key` is
  `dynamic/github-code-scanning/codeql:analyze`, i.e. default setup — and a config
  file is only read when a workflow passes `config-file` to `codeql-action/init`.
- **Inline `# codeql[rule-id]` comments** don't work either. GitHub does not
  natively dismiss on them; that is why `advanced-security/dismiss-alerts` exists,
  and it needs SARIF that default setup never hands you.

Converting to advanced setup to silence a query repo-wide — including at future
sites where a real traceback could leak — costs more than an occasional
30-second dismissal. If it ever becomes the right call, it's a design question
for the user, not a unilateral change to their security tooling.

## 6. Dismissing, mechanically

```
gh api --method PATCH repos/randomphrase/harmonist/code-scanning/alerts/N \
  -f state=dismissed \
  -f dismissed_reason="won't fix" \
  -f dismissed_comment="… see #63."
```

- `dismissed_reason` must be one of `false positive`, `won't fix`, `used in tests`.
- **`dismissed_comment` is capped at 280 characters** and the API rejects the
  whole call over it. Draft short; count before sending.
- Reuse the wording already on the sibling alerts so the family reads
  consistently, and always cite the issue.
- Long rationale goes in an issue comment, not the dismissal.

## Before you dismiss

1. Have you listed the alerts and found the sibling that already settled this?
2. Does the flagged line match the family in §2 — or is it genuinely new code?
3. If you verified a claim to repeat in the comment (e.g. "goes through
   `host_is()`"), did you check that function still exists?
4. Is there a real defect underneath the accepted risk, worth its own issue?
5. Is the comment under 280 characters and does it cite the issue?
