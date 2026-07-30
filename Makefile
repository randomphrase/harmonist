.PHONY: css css-watch test coverage run lint format typecheck template-lint check e2e

# Pin the Tailwind standalone binary so `make css` is byte-reproducible across
# machines + CI — pytailwindcss otherwise downloads 'latest', whose minified
# output drifts from the committed bundle (breaks the CI css-drift check).
TAILWINDCSS_VERSION ?= v4.2.4
export TAILWINDCSS_VERSION

# Build the production CSS bundle. Re-run whenever templates change.
css:
	tailwindcss -i static/input.css -o static/harmonist.css --minify

# Watch templates and rebuild CSS on save.
css-watch:
	tailwindcss -i static/input.css -o static/harmonist.css --watch

test:
	pytest test/

# Test line coverage of the package.
coverage:
	coverage run --source=harmonist -m pytest -q && coverage report

# Ruff lint (idioms, bugs, import order). Add ARGS=--fix to autofix.
lint:
	ruff check $(ARGS) src test

# Ruff formatter (Black-compatible). Add ARGS=--check to verify only.
format:
	ruff format $(ARGS) src test

# Verify formatting only — same as CI's `ruff format --check`. Run `make
# format` to apply. Part of `check` so format drift is caught locally, not
# only in CI.
format-check:
	ruff format --check src test

# mypy strict type check.
typecheck:
	mypy

# Forbid onclick alongside hx-* on one element (the #40 race) — see
# scripts/lint_templates.py for the rationale.
template-lint:
	python3 scripts/lint_templates.py

# Everything CI would gate on (lint + format + types + tests).
check: lint format-check typecheck template-lint test

# Browser smoke tests (opt-in): pip install -e .[e2e] && playwright install chromium
e2e:
	RUN_E2E=1 pytest test/e2e/

# Local dev server. Set HARMONIST_MUSIC_DIR / HARMONIST_CONFIG_DIR as needed.
run:
	uvicorn harmonist.web.main:app --reload
