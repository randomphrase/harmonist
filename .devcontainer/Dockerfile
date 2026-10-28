FROM ghcr.io/astral-sh/uv:0.13.6 AS uv
# Matches the release image's Python 3.15 on trixie. "dev-" tags are unreleased
# builds of devcontainers/images; move to "3.15-trixie" once it is published.
FROM mcr.microsoft.com/devcontainers/python:dev-3.15-trixie

COPY --from=uv /uv /uvx /usr/local/bin/

USER root
RUN uv venv --seed --python /usr/local/bin/python3 /opt/venv \
    && chown -R vscode:vscode /opt/venv \
    && install -d -o vscode -g vscode /home/vscode/.cache /home/vscode/.cache/uv

# Docker exec / TRAMP must see the same Python tools as interactive terminals.
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
# uv sync / uv run default to ./.venv, which is the host's venv on the bind mount.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
# The uv cache is a named volume, so hardlinks into /opt/venv would cross filesystems.
ENV UV_LINK_MODE=copy
COPY profile.sh /etc/profile.d/99-harmonist.sh
USER vscode
