---
name: release
description: Cut a Harmonist release. Use when the user asks to "cut 1.5", "do a release", "tag a version", or "ship what's on main". Covers auditing the changelog against the log, re-trimming its entries to something scannable, the version bump, the release commit and its message, the signed tag, the GitHub Release (the step that gets forgotten), and the workflows the tag triggers.
---

# Cutting a release

A release is four artifacts that must agree: the `CHANGELOG.md` section, the
`pyproject.toml` version, the signed tag, and the **GitHub Release**. Getting
three of four is the normal failure — see step 8.

Releases are exempt from `issue-first`: rolling a changelog and bumping a version
can't change runtime behavior. They still need `make check` to pass.

## Before you start

Be on `main`, clean, and level with `origin`. Then pick the number from what's in
`## [Unreleased]`, per semver:

- a non-empty `### Added` → **minor** (`1.4.0` → `1.5.0`)
- only `### Fixed` / `### Changed` → **patch**
- a breaking change to the sidecar schema, config, or the tagging contract →
  major, and stop to discuss it first

## 1. Audit the changelog against the log

**Don't skip this, and don't do it by reading the changelog alone.** Read the
commits and check each one either maps to an entry or is genuinely internal:

```
git log --oneline vPREV..main
```

1.5.0 nearly shipped without #112 ("an unreadable audio file reads as *this
album has no tags*") — a real user-facing fix. It got missed because it landed
across two commits whose subjects read as internal (`Stop an unreadable file
reading as an untagged one`), so neither author-moment felt like a changelog
moment. The changelog looked complete because everything *in* it was correct.

Anything missing gets its entry now, in the release commit. This is the one
allowed exception to the changelog skill's "never invent entries at release
time" — you are recovering an entry that should have existed, not writing notes
retroactively.

## 2. Sweep the docs for staleness

A release is when someone new reads `README.md`, `docs/usage.md` and
`docs/design.md`. Grep them for whatever this cycle removed or renamed — states,
routes, UI surfaces, flags. 1.5.0 was about to ship with `docs/design.md` still
telling users to click Re-tag from MB "in the detail modal", a surface deleted an
hour earlier. `docs/usage.md` is the likeliest to rot: it names buttons.

## 3. Cut every entry back to its claim

**Enforce this on the whole section at once, before rolling it.** It is a gate
because the rule erodes across a cycle: entries are written one at a time, in
the author's moment, when the mechanism is vivid and every caveat feels
load-bearing. Each looks proportionate alone. Twenty stacked into one section
is a wall, and nobody sees the section until release day.

### The bold-claim test

Every entry already contains its own summary — the clause you would bold. **That
clause, as a complete sentence, plus the issue number, IS the entry.** Write it
out and then ask of every surviving word: what does this add?

    - **The Tags comparison covers every album tag Harmonist writes** —
      seventeen, where it compared six (#295).

That is a finished entry. What it replaced ran five lines and added the bug's
history, the wrong "N of M" line it used to print, a list of eleven recovered
field names, and a note about two-column layout — all true, all in #295.

Three things earn a second clause or sentence, and nothing else does:

- **A knob.** `cache_ttl_seconds` under `[musicbrainz]` — the user cannot look
  it up if the entry does not name it.
- **Something they must do or expect on upgrade.** "Albums Harmonist tagged
  itself will show one corrective update."
- **The user-facing name of a thing they must click** — **No more tracks to
  get**, **read again**.

Cut, always: the mechanism (*how* it was fixed is the commit's job), the history
of the bug (they have the fixed version; the broken one is not news), the
counter-case and the caveat, and the second and third example. A vivid example
survives only where it is doing the explaining and the sentence is shorter with
it than without — "the bonus DVD you never ripped".

Punchy is not terse: the voice stays the same, there is just far less of it.

1.10.0 shipped a wall of four-line paragraphs and was rewritten after the fact;
1.12.0 then did it again *under the one-or-two-sentence rule*, because one
sentence with four subordinate clauses passes that rule and still does not scan.
Hence a test against the claim rather than a sentence count. Accurate is not the
bar, and neither is short — scannable is.

## 4. Roll the changelog and bump the version

Per the `changelog` skill: rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`
and add a fresh empty `## [Unreleased]` above it. Then `pyproject.toml` — the
single source of the version:

```
version = "X.Y.Z"
```

The trim applies to the bullets. Detail is not deleted, it is *relocated* — to
the issue and to the commit that made the change, both of which are already
linked from the bullet and neither of which has a length limit.

## 5. `make check`

## 6. The release commit

Subject is exactly `Release X.Y.Z`. Stage explicit paths — `CHANGELOG.md`,
`pyproject.toml`, and any docs from step 2.

The body is not a summary of the changelog; it's the part the changelog can't
carry — which on a quiet release is just the first two lines below. `git show
v1.4.0` and `git show v1.5.0` show the long shape, from cycles that had one to
justify; read them for the voice, not as a quota to fill.

1. One line on what the commit mechanically does.
2. Why this bump, per semver.
3. **Themes, only if there are any** — see below. Most releases stop at 2.

### Themes are the exception, not the format

A theme is prose that names what got *better* across several entries at once.
It has to earn its place, and the bar is: **something true of the release that
no bullet and no linked commit says.** If a reader would learn nothing from it
that the six bullets above it already told them, it is filler — cut it and ship
the bullets alone. That is the normal outcome for a patch release.

What does not clear the bar:

- **The obvious.** A bugfix release is about bugs; a minor release adds things.
  Restating the section heading in paragraph form is not a theme.
- **A retelling of one bullet.** If a theme names one issue, it is that issue's
  commit message with the line breaks moved, and the commit is already better
  at it — this repo writes long, careful ones.
- **A group with nothing to say about the group.** Three fixes that happen to
  touch the same feature are three bullets. The theme needs a point about the
  *set*: they shared a cause, or fixing them changed how the feature works.

What does: a cross-cutting change several bullets only hint at individually;
something the user must do or know on upgrade; a capability that arrived in
pieces no single entry describes; the reason a release exists at all.

Two is the ceiling in practice. 1.10.2 shipped three for six unrelated fixes,
each one a paraphrase of a commit message — 1.5KB of prose in front of notes a
reader could have scanned in fifteen seconds. Where there is nothing to say,
say nothing: bullets alone is a finished release, not a lazy one.

## 7. The signed tag

```
git tag -s vX.Y.Z -m "Harmonist X.Y.Z"
git tag -v vX.Y.Z          # verify BEFORE pushing
```

**Stop here and ask** — see the `source-control` skill. Everything up to this
point is local and undoable; everything after it is public and is not, and
invoking this skill is not authorization to cross that line. Say the commit and
the verified tag are ready, and wait.

Once they say go, push the commit first, then the tag — a tag whose commit isn't
on the remote publishes an image from a commit nobody can fetch:

```
git push origin main
git push origin vX.Y.Z
```

One go-ahead covers the rest of this release — the tag push and step 8's
`gh release create` — and nothing beyond it.

## 8. The GitHub Release — the step that gets forgotten

The tag is not the release. Every prior version has one (`gh release list`), and
1.5.0 shipped without it until the user noticed.

```
gh release create vX.Y.Z --verify-tag --title "Harmonist X.Y.Z" --notes-file -
```

The body is the changelog section:

- the `### Added` / `### Changed` / `### Fixed` sections, each bullet unwrapped
  to a single line (the changelog hard-wraps; GitHub doesn't need it), with
  `### Changed` led by the most significant entry

For most releases that is the whole body, and it is complete as it stands. Only
if the release commit ended up with themes (step 6) do they follow, after a
`---` rule, with the theme leads in **bold**. No themes in the commit, no rule
and no narrative here — do not write one for the occasion.

Never `--generate-notes`. A list of commit subjects is exactly what the
changelog exists to not be. `gh` marks the newest release latest on its own.

## 9. Watch what the tag triggered

The tag fires `Publish image` (`.github/workflows/publish.yml`) alongside the
usual CI, pushing `ghcr.io/randomphrase/harmonist` at `:X.Y.Z`, `:X.Y`, `:X`
and `:latest`. **`linux/amd64` only** — the Synology NAS is the target and it
only pulls; an arm64 dev box builds from source instead.

```
gh run list --limit 6
```

Confirm from the workflow run, not the registry: reading published tags via
`gh api` needs a `read:packages` scope this setup doesn't have.

## Done when

- [ ] every commit since the last tag is in the changelog or genuinely internal
- [ ] `README.md`, `docs/usage.md` and `docs/design.md` describe what shipped
- [ ] **every changelog bullet is its bold claim as a sentence**, plus a knob or
      an upgrade consequence where there is one — mechanism, bug history and
      caveats left to the issue and the commit
- [ ] any theme survives "what does this say that the bullets don't?" — if
      none does, the release notes are the bullets alone
- [ ] `pyproject.toml` bumped, `make check` green
- [ ] `Release X.Y.Z` commit, GPG-signed tag verified locally
- [ ] commit pushed, then tag
- [ ] **GitHub Release published** with hand-written notes
- [ ] CI and `Publish image` both green on the tag
