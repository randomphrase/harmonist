# Harmonist — working notes for Claude

A self-hosted Bandcamp → MusicBrainz → Picard-compatible music tagger
(destinations: Plex / Navidrome).

This file is the *how to work in this repo* brief. To keep it from rotting, it
deliberately does **not** restate anything documented elsewhere — read those,
then follow the conventions below.

## Read these first (don't duplicate them here)

- **`README.md`** — what Harmonist is and where it fits, running it (Docker /
  from source / demo mode), configuration, deployment & security, the tech stack.
- **`docs/design.md`** — the design spec and source of truth for *how it's meant
  to work*: use cases, the album **state machine** (states + transition diagram),
  the **sidecar schema**, the tagging contract, cover art, the **module map**,
  and the adoption/linking mechanics (backfill, surrender, potential downloads).
- **`.claude/skills/review-gate`** — the design-invariant checklist (below).

Reach for `docs/design.md` before answering anything about states, the sidecar,
matching/linking, or tagging — don't reconstruct it from memory or code alone.

## Review gate — before every commit

Consult the **review-gate** skill before every commit, and before declaring any
implementation task complete. It checks the `docs/design.md` invariants that
tests and `mypy` can't see — audit coverage; no-guessing / exact-scoped-unique
matching; state is *derived*, never stored; non-destructive to user data; an
escape hatch out of every state; the MusicBrainz call budget; idempotent
transitions. Don't skip it because a diff "looks small" — small diffs are where
invariants erode. (This skill supersedes the old inline "sidecar minimalism" and
persistence rules — the load-bearing-fields-only rule now lives in gate item 3.)

## Working conventions

- **Quality gate:** `make check` (ruff + `ruff format --check` + `mypy --strict`
  + pytest) must pass before you commit; CI runs the same on Python 3.12/3.13.
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

