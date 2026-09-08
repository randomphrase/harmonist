# Development with a devcontainer and Emacs

The workspace provides Python 3.14, Git, Make, zsh, and Oh My Zsh. Emacs stays
on the host, including when managed by Nix Home Manager. The container user is
`vscode` (UID/GID 1000); this matches the initial developer's host user. If your
host uses another UID/GID, adjust the image user before using direct Compose.

## Host prerequisites

Install [Docker Engine](https://docs.docker.com/engine/install/) and its Compose
plugin on the machine holding this checkout. On Ubuntu, install the buildx
plugin too, which Compose uses for builds:

```sh
sudo apt install docker.io docker-compose-v2 docker-buildx
```

Check that these work as your user:

```sh
docker version
docker compose version
docker run --rm hello-world
```

Home Manager can supply client tools, but the host also needs a running Docker
daemon. No host Python environment, Node, or Dev Container CLI is required for
the Compose workflow below.

## Create the workspace

From the repository root:

```sh
docker compose -f .devcontainer/compose.yaml up -d --build
docker exec harmonist-dev bash .devcontainer/setup.sh
docker exec -it harmonist-dev zsh
```

The last command opens an interactive zsh session with Oh My Zsh. Dependencies
are installed in `/opt/venv`, which is on PATH for both terminals and Docker exec.
The checkout is mounted at `/workspaces/harmonist`; edits persist on the host.
The production Dockerfile and root Compose file are for deploying the app.

Inside the container:

```sh
make check
make demo
```

Open http://localhost:8000 on the Docker host. The port is published on host
loopback only. Demo mode supplies sample albums and mocks Bandcamp/MusicBrainz;
no account or music files are needed. `make demo` enables live reload and keeps
demo settings in `.dev-data/demo-config/`. Stop the server with Ctrl+C. From
Emacs, use `C-x p c` with `make demo` and `M-x kill-compilation` to stop it.

For ordinary development, run
`uvicorn harmonist.web.main:app --reload --host 0.0.0.0`. Music goes in `music/`
and config in `.dev-data/config/`, both ignored by Git and preserved on rebuild.
Use copies of albums when testing tagging or file moves. Run `make css` after
template changes, or `make css-watch` in another container terminal. The pinned
Tailwind binary downloads on first use.

## Restart and rebuild

```sh
# Stop / resume the existing workspace:
docker compose -f .devcontainer/compose.yaml stop
docker compose -f .devcontainer/compose.yaml start

# Rebuild after changing the Dockerfile, profile.sh, or zshrc:
docker compose -f .devcontainer/compose.yaml up -d --build
docker exec harmonist-dev bash .devcontainer/setup.sh
```

Rerun the setup command after changing `pyproject.toml`, too. Dependencies in
`/opt/venv` must be reinstalled when the container is recreated. The fixed name
`harmonist-dev` supports one workspace at a time; change it and the published
port if you need multiple checkouts running simultaneously.

VS Code users can also use **Dev Containers: Reopen in Container**; the included
`devcontainer.json` uses the same Compose service and runs setup automatically.
