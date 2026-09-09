---
name: schema-migration
description: How to evolve the SQLite schema in src/harmonist/activity_store.py. Consult BEFORE adding, renaming, or removing any column, table, or index there — and before adding a second SQLite store. Harmonist ships to self-hosted users, so a migration that lands wrong corrupts a database on someone's NAS that you cannot reach; the rules that keep upgrades (and downgrades) safe are collected here.
---

# SQLite schema migrations

`activity.db` lives in the user's config dir on their own hardware. There is no
staging environment, no ops team, and no way to fix a bad migration after the
fact — an upgraded Harmonist opens whatever file is already there. Treat the
schema as a published interface.

The mechanism is deliberately dependency-free: SQLite's built-in
`PRAGMA user_version` plus an append-only tuple of forward migrations in
`activity_store.py`. That's the stdlib equivalent of Rails migrations, sized to
one small store.

## 1. A schema change is always a NEW appended migration

`_MIGRATIONS[i]` takes the database from `user_version` `i` to `i+1`.

- **Never edit a shipped entry.** A user whose DB is already past that version
  will never re-run it, so their schema silently diverges from a fresh install's.
  The two databases then disagree forever, and nothing detects it.
- **Never reorder or delete entries**, for the same reason.
- **Never renumber.** `SCHEMA_VERSION = len(_MIGRATIONS)` — the version is derived
  from the tuple's length, so appending is the only correct edit.

The only time an entry may change is before it has ever been released. If you're
unsure whether it shipped, assume it did.

## 2. Write forward-only, additive DDL

SQLite's `ALTER TABLE` is limited, and destructive changes are unrecoverable on
a user's machine.

- **Add**, don't rewrite: `ALTER TABLE events ADD COLUMN album_id TEXT` is the
  model. New columns must be **nullable** or have a default — existing rows can't
  be back-filled with information nobody recorded.
- Dropping or renaming a column means the 12-step table rebuild. Don't, unless
  there is a real reason; a dead column costs nothing.
- Add the index in the same migration as the column it serves.
- Statements run **exactly once**, tracked by `user_version` — so don't write
  `IF NOT EXISTS` defensively. If a migration needs that to be safe, it's a sign
  the versioning has already gone wrong somewhere else.

## 3. Keep each step atomic, and refuse the future

`_migrate()` wraps each step's DDL *and* its `user_version` bump in one
`BEGIN`/`COMMIT`, rolling back on failure. That's what makes an interrupted
upgrade resumable rather than half-applied. Preserve that shape.

It also refuses to open a database whose `user_version` exceeds this build's
`SCHEMA_VERSION`. A user who downgrades Harmonist must not have an older binary
write rows against a schema it doesn't understand — `init()` catches the refusal,
degrades to an in-memory store, and logs loudly. The app still starts; history
just stops persisting until they upgrade again. **Degrade, never crash, never
corrupt** — same non-destructive instinct as the rest of the codebase.

## 4. The new column must be load-bearing

The `review-gate` rule about sidecar fields applies here too: a persisted column
needs at least one reader and must drive a concrete affordance. `album_id` earned
its place by being written on *both* sources, so one query returns an album's
whole history — the foundation for per-album history (#14).

Ask before appending: what query does this enable, and what does the user see as
a result? "We might want it later" is not an answer — the migration is append-only,
so a speculative column is permanent.

## 5. Test the upgrade path, not just the result

A test that opens a fresh database proves nothing about the case that matters.
Cover:

- **Upgrade in place:** build a DB at version `N-1` (apply a prefix of
  `_MIGRATIONS` by hand), open it through `init()`, assert `user_version` reached
  `SCHEMA_VERSION` and that pre-existing rows survived intact with the new column
  reading `NULL`.
- **Idempotent re-open:** opening an already-current DB applies nothing and
  changes no data.
- **Downgrade guard:** a DB stamped at `SCHEMA_VERSION + 1` degrades to in-memory
  instead of raising out of `init()`.

## 6. Before committing

- Appended, not edited — confirm by diffing `_MIGRATIONS`: existing entries must
  be untouched.
- `SCHEMA_VERSION` still derived from `len(_MIGRATIONS)`, not hardcoded.
- New column nullable or defaulted; index added alongside.
- Upgrade-path test present.
- The column has a reader.
- Schema changes are functional work — issue and PR (`issue-first` skill), and
  run `review-gate`.
