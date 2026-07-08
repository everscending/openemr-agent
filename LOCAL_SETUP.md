# Local Setup

How to get OpenEMR running locally for development. The recommended path uses
Docker — the only host dependency is Docker itself; the `openemr` container
provides PHP, Node, Composer, the test runners, and all validation tooling.

For the full contributor workflow (worktrees, pre-commit hooks, Xdebug, API
testing, snapshots, and more), see [CONTRIBUTING.md](CONTRIBUTING.md). For
day-to-day commands and coding standards, see [CLAUDE.md](CLAUDE.md).

## Prerequisites

1. **[Docker](https://docs.docker.com/install/)** with
   **[Docker Compose](https://docs.docker.com/compose/install/)**.
2. **[`openemr-cmd`](https://github.com/openemr/openemr-devops/tree/master/utilities/openemr-cmd)**
   (recommended) — the canonical CLI for the dev environment. It runs from any
   directory, dispatches commands into the running openemr container, manages
   git worktrees, and installs pre-commit hooks. It lives in the separate
   `openemr/openemr-devops` repo; quick install to any directory on your PATH
   (e.g. `~/.local/bin`):
   ```sh
   curl -fsSL https://raw.githubusercontent.com/openemr/openemr-devops/master/utilities/openemr-cmd/openemr-cmd -o ~/.local/bin/openemr-cmd
   curl -fsSL https://raw.githubusercontent.com/openemr/openemr-devops/master/utilities/openemr-cmd/openemr-cmd-h -o ~/.local/bin/openemr-cmd-h
   chmod +x ~/.local/bin/openemr-cmd ~/.local/bin/openemr-cmd-h
   ```
   Verify with `openemr-cmd --version`.
   - **Windows:** `openemr-cmd` is a bash script — install it inside WSL2
     (recommended) or Git Bash. On native cmd.exe/PowerShell, fall back to
     `docker compose exec openemr /root/devtools <cmd>` from
     `docker/development-easy/`.
3. **git** (you already have it if you cloned this repo).

No host PHP, Node, Composer, MySQL, or Python is required for the Docker path.

## Quick Start

From the repository root:

```sh
cd docker/development-easy
openemr-cmd up          # or: docker compose up --detach --wait
```

The first start takes several minutes: the container installs Composer and npm
dependencies, builds frontend assets, and initializes the database. Watch
progress with:

```sh
docker compose logs -f openemr
```

It's ready when you see:

```
Starting cron daemon!
Starting apache!
```

Then open the app:

| What | URL | Credentials |
|------|-----|-------------|
| OpenEMR (HTTP) | http://localhost:8300/ | `admin` / `pass` |
| OpenEMR (HTTPS, self-signed) | https://localhost:9300/ | `admin` / `pass` |
| phpMyAdmin | http://localhost:8310/ | `openemr` / `openemr` |
| MySQL (direct, e.g. MySQL Workbench) | localhost:8320 | `openemr` / `openemr` (root: `root` / `root`) |
| Mailpit (catches outbound email) | http://localhost:8025/ | — |
| CouchDB GUI (optional document storage) | http://localhost:5984/_utils/ | `admin` / `password` |
| Selenium live view (watch e2e tests) | http://localhost:7900/ | password `openemr123` |

## Making Changes

The repository is bind-mounted into the container, so most edits to PHP,
templates, and JS appear after a browser refresh — no rebuild or restart
needed.

Exceptions:

- **Theme/SCSS changes** (`interface/themes/`): rebuild themes and clear the
  browser cache:
  ```sh
  openemr-cmd build-themes
  ```
- **Dependency changes** (`composer.json` / `package.json`): `vendor/` and
  `node_modules/` live in named Docker volumes, so run installs inside the
  container, e.g. `openemr-cmd e 'composer install'`.

## Stopping and Cleaning Up

```sh
cd docker/development-easy

docker compose stop     # pause containers, keep everything (fastest restart)
docker compose down     # remove containers, keep volumes (db/vendor cached)
openemr-cmd down        # full teardown: removes volumes too (docker compose down -v);
                        # next `up` rebuilds from scratch
```

## Verifying Your Setup

Run the test suites inside the container (works from any directory):

```sh
openemr-cmd unit-test           # alias: ut — fastest DB-backed check
openemr-cmd phpunit-isolated    # alias: pit — no-database isolated tests
openemr-cmd clean-sweep-tests   # alias: cst — all automated test suites
openemr-cmd php-log             # alias: pl — tail the PHP error log
```

See CLAUDE.md's "Testing" and "Code Quality" sections for the full command
list (phpstan, phpcs, rector, eslint, etc.).

## Resetting / Demo Data

```sh
openemr-cmd dev-reset-install            # wipe and reinstall OpenEMR
openemr-cmd dev-reset-install-demodata   # same, plus demo users and patients
openemr-cmd import-random-patients 100   # generate synthetic patients (synthea)
```

## Alternative Dev Environments

`docker/development-easy` is the default. Two variants exist:

- `docker/development-easy-light` — fewer services, lighter footprint.
- `docker/development-easy-redis` — adds Redis.

Start them the same way (`cd` into the directory, then `openemr-cmd up`).

The default stack runs PHP 8.5 (`openemr/openemr:flex`). To test another PHP
version, change the `openemr` service image in `docker-compose.yml` — e.g.
`openemr/openemr:flex-3.22-php-8.2` for PHP 8.2. See CONTRIBUTING.md for the
full list.

## Working on Multiple Branches (Worktrees)

To develop on several branches concurrently, each with its own docker stack on
non-conflicting ports, use the managed worktree commands — **never raw
`git worktree` commands** against this repo:

```sh
openemr-cmd worktree add my-feature -b --start   # new branch + worktree + stack
openemr-cmd worktree list
openemr-cmd worktree exec my-feature ut          # run commands in its container
openemr-cmd worktree remove my-feature
```

See CLAUDE.md's "Working in a git worktree" section for the rules and
recovery procedures.

## Pre-commit Hooks (Recommended)

One-time install per clone (stack must be running at commit time):

```sh
openemr-cmd prek-install    # alias: pi
```

`git commit` will then validate staged changes (phpstan, rector, phpcs,
codespell, and more) inside the container — no host toolchain needed.

## Troubleshooting

- **App loads unstyled; 404s on `/public/themes/*.css` (e.g.
  `style_light.css`):** the theme CSS was never built. The container's startup
  script only runs the frontend build when `node_modules/` or `public/` is
  missing/empty — if you ever ran `npm install` on the host, the container
  sees them populated and skips the build, leaving `public/themes/` empty.
  Fix by building the themes inside the container:
  ```sh
  openemr-cmd build-themes
  # or: docker compose exec openemr /root/devtools build-themes
  ```
- **`EACCES` / permission errors on bind-mounted files:** use `openemr-cmd`
  consistently for `up` — it exports `HOST_UID`/`HOST_GID` so the in-container
  apache user adopts your host uid. For a checkout with leftover root-owned
  files from older runs: `sudo chown -R "$(id -u):$(id -g)" .` from the repo
  root.
- **Commit fails with "Could not automatically determine target OpenEMR
  container":** the pre-commit hooks route through a running container — start
  the stack (`openemr-cmd up`, or `openemr-cmd worktree up <branch>` for a
  worktree) before committing.
- **Port conflicts:** the default ports (8300, 9300, 8310, 8320, 4444, 7900,
  5984, 6984, 8025, 1025) can be overridden via the `WT_*_PORT` environment
  variables referenced in `docker/development-easy/docker-compose.yml`.
- **Stale/broken state after a failed start:** `openemr-cmd down` (removes
  volumes) then `openemr-cmd up` for a clean rebuild.

## Working Without Docker

If you maintain a full host toolchain (PHP 8.2+, Composer, Node, Python 3),
you can run code-quality checks and isolated tests directly on the host:

```sh
composer install
composer phpunit-isolated    # isolated tests, no database
composer code-quality        # full PHP quality suite
npm install && npm run build # frontend assets
```

Running the full application without Docker requires installing the complete
dependency stack (MySQL/MariaDB, Apache/PHP, etc.) — see
[OpenEMR Development Versions](https://open-emr.org/wiki/index.php/OpenEMR_Installation_Guides#OpenEMR_Development_Versions)
on the wiki, and the "Working without Docker" section of CONTRIBUTING.md.
