# Harmonist — working notes for Claude

A self-hosted Bandcamp → MusicBrainz → Picard-compatible music tagger
(destinations: Plex / Navidrome).

This file is the *how to work in this repo* brief. To keep it from rotting, it
deliberately does **not** restate anything documented elsewhere — read those,
then follow the conventions below.

## Read these first (don't duplicate them here)

- **`README.md`** — the pitch: what Harmonist is, where it fits against Picard /
  Lidarr / beets, the two guarantees it makes, the tech stack. It links out
  rather than explaining; keep it that way.
- **`docs/usage.md`** — the user guide: onboarding an existing library, the
  inbox, syncing, the Library and its filters, an album's page, undo, activity.
  User-visible behavior gets documented **here**, not in the README.
- **`docs/installation.md`** — Docker (incl. Synology/ACL permissions), from
  source, demo mode, the `harmonist.toml` reference, uninstall.
- **`docs/deployment.md`** — the security posture: reverse proxy, allowed hosts,
  built-in Basic auth.
- **`docs/design.md`** — the design spec and source of truth for *how it's meant
  to work*: use cases, the album **state machine** (states + transition diagram),
  the **sidecar schema**, the tagging contract, cover art, the **module map**,
  and the adoption/linking mechanics (backfill, surrender, potential downloads).
- **`.claude/skills/review-gate`** — the design-invariant checklist (below).

Reach for `docs/design.md` before answering anything about states, the sidecar,
matching/linking, or tagging — don't reconstruct it from memory or code alone.

Ten further skills are situational rather than always-on, each written up from
a bug this repo actually paid for — consult them when you're in their territory:
`source-control` (before anything leaves this machine — `git push`, `gh pr`,
`gh release` — and before `git add` or rewriting history),
`screenshots` (adding or re-cropping any image under `docs/`),
`web-ui` (anything under `templates/`), `bulk-refactor` (a mechanical edit across
many call sites), `sidecar` (changing what a `.harmonist.json` *contains* —
adding, retiring or repurposing a field, or touching `CURRENT_SCHEMA_VERSION`),
`schema-migration` (the `activity_store` SQLite schema),
`event-recording` (any `activity.record()` / `audit.record()` call, and any code
that mutates a sidecar, tags, or files on disk), `error-handling` (any `except`
clause, fallback return, or broad `except Exception`), `release` (cutting a
version — the changelog audit, the signed tag, the GitHub Release),
`security-alerts` (any CodeQL / code-scanning alert — triage it before you fix
it; most are re-flags of decisions #63 already made).

## Review gate — before every commit

Consult the **review-gate** skill before every commit, and before declaring any
implementation task complete. It checks the `docs/design.md` invariants that
tests and `mypy` can't see — audit coverage; no-guessing / exact-scoped-unique
matching; state is *derived*, never stored; non-destructive to user data; an
escape hatch out of every state; the MusicBrainz call budget; idempotent
transitions. Don't skip it because a diff "looks small" — small diffs are where
invariants erode. (This skill supersedes the old inline "sidecar minimalism" and
persistence rules — the load-bearing-fields-only rule now lives in gate item 3.)

Gate item 3 decides *whether* a sidecar field should exist; the **sidecar**
skill covers what happens on disk once you change one. Both, for any change to
sidecar contents.

## Working conventions

- **Never push or publish without an explicit say-so.** Commit freely; anything
  that leaves the machine is the user's call, every time — ask, and wait. A skill
  that lists `git push` among its steps is documenting it, not authorizing it.
  See the **source-control** skill for what else doesn't count as authorization,
  plus staging and history-rewrite discipline.
- **Issue-first:** raise substantial/functional work (bug fixes, features,
  behavior changes, non-trivial refactors) as a GitHub issue *before* writing the
  code, and close it from the commit (`Fixes #N`). Only changes that can't alter
  runtime behavior (formatting, lint, comments, tests) skip this — see the
  `issue-first` skill for the dividing line.
- **Quality gate:** `make check` (ruff + `ruff format --check` + `mypy --strict`
  + `template-lint` + pytest) must pass before you commit; CI runs the same on
  Python 3.12/3.13/3.14.
- **A test says what the code does, not what it stopped doing.** Assert an
  *absence* only when some live code path could produce the thing — the reliable
  sign is that the same string is asserted **present** elsewhere under different
  conditions (Confirm & Tag on a suggestion card but not a surrender one; the
  amber badge before acceptance but not after). An absence nothing could produce
  is the removal diff pinned in amber: it can never fail, so it never earns its
  run. Same rule for a "we don't support X yet" list — it either duplicates the
  exhaustive positive check or quietly drifts away from the code, as
  `test_picard_spec.py`'s KNOWN_GAPS did for months. Roadmaps go in a comment
  beside the check that would actually notice.
- **UI work:** before editing anything under `templates/` or `static/input.css`,
  consult the `web-ui` skill — native elements over hand-rolled widgets, never
  `onclick` on an element that also carries `hx-*` (mechanically enforced by
  `make template-lint`), and how to verify behavior the Python suite can't see.
  It expands on the CSS-artifact and test-client notes below.
- **Changelog:** when a change is user-visible, add a one-line entry to
  `CHANGELOG.md` under `[Unreleased]` in the same commit — see the `changelog`
  skill for what counts and how to word it.
- **CSS is a committed build artifact.** After editing *any* template, run
  `make css` and commit the regenerated `static/harmonist.css`. The Tailwind CLI
  is pinned so output is byte-reproducible, and CI fails on drift — a template
  edit without a rebuild is the most common CI break here.
- **Templates and `static/` live at the project root, not under `src/`** —
  `web/main.py` walks up to locate them.
- **Test client + CSRF:** the middleware requires `HX-Request: true` on every
  state-changing request. HTMX sends it in a browser; pytest's `TestClient` does
  not, so fixtures build it as `TestClient(app, headers={"HX-Request": "true"})`.
  New web fixtures must follow suit.
- **Demo mode** (`HARMONIST_DEMO_MODE=1`) monkey-patches MB / Bandcamp / cover-art
  and sandboxes a seeded sample library under `$TMPDIR`; the configured
  `music_dir` is **never** touched. Use it to exercise flows without real traffic.

