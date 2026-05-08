.PHONY: css css-watch test run

# Build the production CSS bundle. Re-run whenever templates change.
css:
	tailwindcss -i static/input.css -o static/harmonist.css --minify

# Watch templates and rebuild CSS on save.
css-watch:
	tailwindcss -i static/input.css -o static/harmonist.css --watch

test:
	pytest test/

# Local dev server. Set HARMONIST_MUSIC_DIR / HARMONIST_CONFIG_DIR as needed.
run:
	uvicorn harmonist.web.main:app --reload
