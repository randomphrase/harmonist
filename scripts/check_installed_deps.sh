#!/bin/sh
# Stop `make check` when the installed packages no longer satisfy pyproject.toml.
#
# A stale environment otherwise shows up as failures that look like bugs in the
# code. The devcontainer installs its packages once, when it is created, so a
# dependency that changes after that goes unnoticed unless something asks uv.
#
# --dry-run matters: worktrees share the devcontainer's environment, and really
# installing from one would repoint Harmonist's editable install at it. That
# repointing is also the one change the dry run reports that isn't staleness.
set -eu
cd "$(dirname "$0")/.."

# Whoever installs with something else has no uv to ask.
command -v uv >/dev/null 2>&1 || exit 0

report=$(uv pip install --color never --dry-run -e '.[dev]' 2>&1) || {
    printf '%s\n' "$report" >&2
    exit 1
}

stale=$(printf '%s\n' "$report" | grep -E '^ [+~] ' | grep -Ev '^ [+~] harmonist[ =]' || true)
[ -n "$stale" ] || exit 0

cat >&2 <<EOF
The installed packages don't satisfy pyproject.toml. Missing or out of date:
$stale

In the devcontainer, from the host:  devcontainer exec bash .devcontainer/setup.sh
Elsewhere:                           uv pip install -e '.[dev,e2e]'
EOF
exit 1
