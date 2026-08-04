---
name: error-handling
description: How Harmonist handles failures — what may be caught, what must propagate, and how loud to be. Consult BEFORE writing any `except` clause, before adding a fallback/default return on error, and whenever you catch `Exception` broadly. Harmonist runs unattended on someone's NAS for months at a time, so a swallowed error is not a small sin: nobody is watching the terminal, and a failure that returns an empty list is indistinguishable from a genuine empty result.
---

# Error handling

Harmonist runs **unattended**. It's a container on a NAS that syncs, tags and
rewrites files while the user is asleep, and they may not open the UI for weeks.
That single fact drives everything here: an error nobody sees is an error nobody
fixes, and by the time it surfaces the evidence is usually gone.

## 1. Never let a failure look like a legitimate result

This is the rule that matters most, and the easiest to break by accident.

```python
except sqlite3.Error:
    return []          # ← a broken database now looks like an empty one
```

The caller renders "Nothing recorded for this album yet". That is not a
degraded answer, it's a **wrong** one — the UI now asserts something false about
the user's library, confidently.

The same shape, worse: `resolve_alias` returning `None` on error means "this id
was never superseded", so `_find_album` concludes the album doesn't exist. One
transient DB error and the user is told their album is gone.

Before writing a fallback value, ask: **can the caller tell this apart from
success?** If not, don't return it — propagate.

## 2. Three categories, three rules

**The user's actual work** (tag this album, sync, write a sidecar) — let it
fail. Propagate, and let the route turn it into a visible error. A half-done
tagging run that reports success is worse than one that stops.

**Incidental to that work** (recording an activity line, warming a cache,
firing a progress callback) — may be caught, so a logging failure can't abort
the operation being logged. But it must be **loud**: `log.exception(...)` or
`log.error(..., exc_info=True)`, never `debug`, never bare `pass`.

**Genuinely moot** — the rare case where nothing is wrong and nothing is lost.
`request_scan()` after the event loop has closed is the model: the app is
shutting down, a rescan is meaningless, and the no-op is *the correct
behaviour*, not a swallowed failure. These need a comment saying why the
operation is moot; if you can't write that sentence, it's category two.

## 3. Be loud in the log, because the log is all there is

Nobody is watching. The UI is not a channel for anything that happens while the
user is away.

- `log.exception()` in an `except` block — it captures the traceback, and the
  traceback is the thing you'll want in three weeks.
- **ERROR for anything that lost data or stopped working.** WARNING for
  something recovered. DEBUG is for things nobody ever needs to know, which is
  almost never true of a failure.
- In a loop, log the first failure and a count — not N identical tracebacks that
  bury everything else.

## 4. Catch the exception you mean

`except Exception` catches typos, `KeyboardInterrupt` subclasses in some code
paths, and bugs you'd rather find in a test. Name the failure you're actually
tolerating: `sqlite3.Error`, `OSError`, `httpx.HTTPError`, `mb_lookup.MBError`.

A broad catch is defensible at a **boundary** — one background pass shouldn't
die because one album is malformed — but then say so, log it, and keep going
with the rest. That's `reconcile_pending_orphans`'s per-album `except`, which is
correct: it logs, counts the failure, and reports it in the summary.

## 5. The audit log is the worst place to be quiet

Its entire value is the claim *"if it isn't here, it didn't happen."* A
silently-dropped audit record doesn't just lose one line, it makes every other
line untrustworthy — you can no longer reason from an absence.

So: an audit write may not abort the operation it describes (category two), but
a failure to record **must** be an ERROR in the log. If Harmonist is writing to
your files and failing to record it, that is exactly the thing you'd want to
know about.

## 6. Degrade visibly, never silently

`activity_store.init()` is the pattern to copy: it refuses a database it can't
understand, falls back to in-memory, and **logs loudly** that history will not
persist. The app still starts; the user is told what they've lost.

The failure mode to avoid is degrading into a state that looks normal. If
Harmonist is running without durable history, the user should be able to find
that out without reading source.

## Before you commit

1. Does any `except` return a value a caller could mistake for success?
2. Is every catch either loud, or accompanied by a comment saying why the
   operation is genuinely moot?
3. Are you catching a named exception rather than `Exception`?
4. If this fails at 3am on a NAS, how does the user ever find out?
