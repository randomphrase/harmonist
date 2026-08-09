---
name: release
description: Cut a Harmonist release. Use when the user asks to "cut 1.5", "do a release", "tag a version", or "ship what's on main". Covers auditing the changelog against the log, the version bump, the release commit and its message, the signed tag, the GitHub Release (the step that gets forgotten), and the workflows the tag triggers.
---

# Cutting a release

A release is four artifacts that must agree: the `CHANGELOG.md` section, the
`pyproject.toml` version, the signed tag, and the **GitHub Release**. Getting
three of four is the normal failure — see step 7.

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

A release is when someone new reads `README.md` and `docs/design.md`. Grep them
for whatever this cycle removed or renamed — states, routes, UI surfaces,
flags. 1.5.0 was about to ship with `docs/design.md` still telling users to
click Re-tag from MB "in the detail modal", a surface deleted an hour earlier.

## 3. Roll the changelog and bump the version

Per the `changelog` skill: rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`
and add a fresh empty `## [Unreleased]` above it. Then `pyproject.toml` — the
single source of the version:

```
version = "X.Y.Z"
```

## 4. `make check`

## 5. The release commit

Subject is exactly `Release X.Y.Z`. Stage explicit paths — `CHANGELOG.md`,
`pyproject.toml`, and any docs from step 2.

The body is not a summary of the changelog; it's the part the changelog can't
carry. Read `git show v1.4.0` and `git show v1.5.0` for the established shape:

1. One line on what the commit mechanically does.
2. Why this bump, per semver.
3. **The themes** — two or three, in prose, each naming its issues. A theme
   explains what got *better* and why it mattered, which a bullet list of
   effects can't. Fixes that share a cause belong in one theme.

## 6. The signed tag

```
git tag -s vX.Y.Z -m "Harmonist X.Y.Z"
git tag -v vX.Y.Z          # verify BEFORE pushing
```

Push the commit first, then the tag — a tag whose commit isn't on the remote
publishes an image from a commit nobody can fetch:

```
git push origin main
git push origin vX.Y.Z
```

## 7. The GitHub Release — the step that gets forgotten

The tag is not the release. Every prior version has one (`gh release list`), and
1.5.0 shipped without it until the user noticed.

```
gh release create vX.Y.Z --verify-tag --title "Harmonist X.Y.Z" --notes-file -
```

The body is the changelog section **plus** the commit's themes:

- the `### Added` / `### Changed` / `### Fixed` sections, each bullet unwrapped
  to a single line (the changelog hard-wraps; GitHub doesn't need it), with
  `### Changed` led by the most significant entry
- a `---` rule
- the themed narrative from the release commit, with the theme leads in **bold**

Never `--generate-notes`. A list of commit subjects is exactly what the
changelog exists to not be. `gh` marks the newest release latest on its own.

## 8. Watch what the tag triggered

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
- [ ] `README.md` and `docs/design.md` describe what actually shipped
- [ ] `pyproject.toml` bumped, `make check` green
- [ ] `Release X.Y.Z` commit, GPG-signed tag verified locally
- [ ] commit pushed, then tag
- [ ] **GitHub Release published** with hand-written notes
- [ ] CI and `Publish image` both green on the tag
