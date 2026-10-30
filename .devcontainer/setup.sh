#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
uv pip install -e '.[dev,e2e]'
# The browser builds the pinned Playwright drives, and their system libraries,
# so `make e2e` runs here in both engines CI runs.
playwright install --with-deps chromium webkit
make browser-assets
mkdir -p music .dev-data/config
