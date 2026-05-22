.PHONY: css css-watch test run lint format typecheck check

# Build the production CSS bundle. Re-run whenever templates change.
css:
	tailwindcss -i static/input.css -o static/harmonist.css --minify

# Watch templates and rebuild CSS on save.
css-watch:
	tailwindcss -i static/input.css -o static/harmonist.css --watch

test:
	pytest test/

# Ruff lint (idioms, bugs, import order). Add ARGS=--fix to autofix.
lint:
	ruff check $(ARGS) src test

# Ruff formatter (Black-compatible). Add ARGS=--check to verify only.
format:
	ruff format $(ARGS) src test

# mypy strict type check.
typecheck:
	mypy

# Everything CI would gate on.
check: lint typecheck test

# Local dev server. Set HARMONIST_MUSIC_DIR / HARMONIST_CONFIG_DIR as needed.
run:
	uvicorn harmonist.web.main:app --reload
