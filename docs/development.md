# Development with a devcontainer and Emacs

The workspace provides Python 3.15, uv, Node 26 with npm, Git, Make, zsh, and Oh My Zsh.
It is defined by `.devcontainer/devcontainer.json`, so the
[Dev Container CLI](https://github.com/devcontainers/cli) and VS Code build and
run it the same way. Emacs stays on the host, including when managed by Nix Home
Manager. The container user is `vscode`; on Linux hosts the CLI matches its
UID/GID to yours, so files written in the checkout keep your ownership.

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

The Dev Container CLI needs Node and npm on the host:

```sh
npm install -g @devcontainers/cli
devcontainer --version
```

Home Manager can supply client tools, but the host also needs a running Docker
daemon. No host Python environment is required.

## Create the workspace

From the repository root:

```sh
devcontainer up
devcontainer exec zsh
```

`devcontainer up` builds the image, starts the container `harmonist-dev`, and
runs `.devcontainer/setup.sh` when the container is first created. Setup installs
the dependencies in `/opt/venv`, which is on PATH for terminals, login shells, and
Docker exec, and builds the browser assets. The checkout is mounted at
`/workspaces/harmonist`; edits persist on the host. The production Dockerfile and
root Compose file are for deploying the app.

The second command opens zsh with Oh My Zsh. Personal shell configuration
belongs in a dotfiles repository rather than the image:
`devcontainer up --dotfiles-repository <url>` installs it in each new container.

Inside the container:

```sh
make check
make demo
```

`make check` and `make test` run the Python suite across four processes, matching
CI, and report the slowest test phases. `make check TEST_WORKERS=0` runs serially
for debugging or a machine with less memory. Each supported Python version's CI
job retains a `pytest-results-<version>` JUnit artifact, including successful runs.
These checks run without browser dependencies; `make e2e` remains separate.

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
docker stop harmonist-dev
devcontainer up

# Rebuild after changing anything in .devcontainer/:
devcontainer up --remove-existing-container

# Reinstall after changing pyproject.toml or package-lock.json:
devcontainer exec bash .devcontainer/setup.sh
```

A rebuild creates a new container, and setup runs again in it; `/opt/venv` does
not survive the old container. The uv download cache lives in the
`harmonist-uv-cache` volume, so reinstalling is quick. The fixed name
`harmonist-dev` supports one workspace at a time; change it and the published
port in `devcontainer.json` if you need multiple checkouts running
simultaneously.

VS Code users can also use **Dev Containers: Reopen in Container**, which reads
the same `devcontainer.json`.

## Browser dependencies

HTMX and Idiomorph are pinned in `package.json` and `package-lock.json`.
The production Docker build installs them in a separate Node stage and copies
only their browser scripts and licenses into the Python
image. The app serves those files locally; Node and `node_modules` are not
runtime dependencies.

The devcontainer setup builds the same files. For development outside the
container, Node 24 or newer and npm are needed once after cloning, and after
changes to the lockfile:

```sh
make browser-assets
```

This runs `npm ci` with package lifecycle scripts disabled, then copies the
locked distributions into `static/vendor/`. That directory and `node_modules/`
are generated and ignored by Git. The template uses the existing static-file
cache versioning so a new build refreshes the browser's scripts.

Dependabot checks the npm packages weekly, grouping minor and patch updates.
Its PRs change the manifest and lockfile; the ordinary container and browser
test builds then use those new versions. No generated JavaScript is committed
or manually refreshed.

For a deliberate version change:

```sh
npm install --save-exact --ignore-scripts htmx.org@<version> idiomorph@<version>
npm run build
make check
make e2e
```

Commit the manifest and lockfile together. `npm ci` checks that they agree
and verifies package integrity. The browser suite exercises the generated
scripts with external requests blocked as well as the app's normal interactions.

## Browser tests and timings

CI runs the browser job in a digest-pinned Playwright image containing the
browser builds and their system libraries, with Python 3.15 selected separately
by `setup-python`. It does not install OS packages or download browsers per run.
When upgrading Playwright in `pyproject.toml`, update the image version and digest
in `.github/workflows/ci.yml` together; the job checks that their versions match.
Python dependency caches are separate for each test job and interpreter, so the
small CSS install cannot populate the cache used by the test suites.

The devcontainer's setup installs the e2e extras and their matching browser
builds, so the suite runs there once the container is created. After a
Playwright upgrade, install them again:

```sh
uv pip install -e ".[dev,e2e]"
playwright install --with-deps chromium webkit
E2E_TRACE_DIR=/tmp/e2e-traces make e2e
```

`make e2e` uses the same four-worker scheduling as CI. To save a JUnit timing
report as well:

```sh
RUN_E2E=1 E2E_TRACE_DIR=/tmp/e2e-traces pytest test/e2e -q -rs \
  -n 4 --dist loadscope --durations=30 --junitxml=/tmp/e2e-results.xml
```

The duration report separates slow setup, test calls, and teardown. JUnit times
include all three phases. CI retains the `e2e-results` artifact on successful
and failed runs, and `e2e-traces` contains traces of failing tests. Summed test
times are worker time, not elapsed time: four tests can run concurrently.
Compare the same revision, dependency versions, worker count, and tracing mode.

Each worker reuses its Playwright driver and browser processes through
`browser_factory`. Each `browser.new_page(...)` creates an isolated context;
the fixture closes contexts even after a failed assertion. Passing tests drain
active route handlers; failed tests discard abandoned callbacks so cleanup cannot
deadlock behind the failure. Server lifetimes and library resets remain fixture-scoped.

Waits observe requests, DOM conditions, or scoped HTMX lifecycle events.
`htmx_complete` arms an observation before an action and follows that request
through its completion; `htmx_ready` waits for a particular control to be wired.
Timer tests use Playwright's clock. The
[testing skill](../.agents/skills/testing/SKILL.md) describes the alternatives
to fixed sleeps, including positive controls for negative assertions. Check
changed synchronization repeatedly with `E2E_LATENCY_MS=300` and four workers.
