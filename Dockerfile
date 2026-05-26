# Harmonist — Bandcamp → MusicBrainz → Plex tagging tool.
#
# The CSS bundle (static/harmonist.css) is pre-built and committed, so the
# image needs no Node/Tailwind toolchain — just Python + the runtime deps.
#
# Multi-arch build (amd64 for most Synology NAS, arm64 for Pi / newer NAS):
#   docker buildx build --platform linux/amd64,linux/arm64 -t harmonist:latest .
FROM python:3.12-slim

ENV HARMONIST_MUSIC_DIR=/music \
    HARMONIST_CONFIG_DIR=/config \
    HARMONIST_HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Copy the whole package + root-level templates/static. main.py locates
# templates/ and static/ by walking up from its own path to the project root
# (/app), so we install editable and run from here.
COPY pyproject.toml LICENSE ./
COPY src ./src
COPY templates ./templates
COPY static ./static

RUN pip install -e .

# Library + config live on bind mounts (see docker-compose.yml). Created so the
# defaults resolve even before anything is mounted.
RUN mkdir -p /music /config
VOLUME ["/music", "/config"]

EXPOSE 8000

# slim has no curl; use Python for the healthcheck.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1

CMD ["uvicorn", "harmonist.web.main:app", "--host", "0.0.0.0", "--port", "8000"]
