# Contributing to cyris

This guide is for people who want to change cyris. Coding agents read
[AGENTS.md](AGENTS.md) instead.

## Setup

You need:

- Python 3.12 or newer and [uv](https://github.com/astral-sh/uv).
- [bun](https://bun.sh), which installs the JavaScript packages at the repo root and in
  `workers/rss/`.
- Node.js on `PATH`, which runs the Worker test suites.
- `uvx` on `PATH`. The end-to-end suite starts mitmproxy through it, and the first run
  downloads mitmproxy, so that run needs network access.

Install the Python dependencies:

```sh
uv sync --dev
```

`scripts/check.sh` runs both `bun install`s itself. To run cyris on your machine, follow
[docs/install-local.md](docs/install-local.md).

## The gate

```sh
scripts/check.sh
```

It runs everything CI runs, plus ruff over `scripts/`. In order, it runs
`bun install --frozen-lockfile` at the root and in `workers/rss/`, then `ruff check` and
`ruff format --check` over `src/`, `tests/` and `scripts/`, then `uv run pytest`. Pytest
also drives both Worker JavaScript suites and the end-to-end suite, which runs
`cyris run` against fakes behind a local mitmproxy.

Outside CI, a test whose tool is missing skips instead of failing: a Worker suite
without its `bun install`, or an entrypoint test without `dash`. A local green run
without them has not run those tests. The end-to-end suite fails without `uvx`; it does
not skip.

While you work, run a subset:

```sh
# One level: unit, integration or e2e
uv run pytest -m unit

# Unit tests, leaving out the guard files
uv run pytest -m "unit and not guard"

# The end-to-end suite
uv run pytest -m e2e

# One file
uv run pytest tests/test_config.py

# One test
uv run pytest tests/test_config.py::TestLoadConfig::test_load_example_configs -v

# Lint and format checks, over what the gate checks
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/

# Fix what ruff can fix
uv run ruff check --fix src/ tests/ scripts/
uv run ruff format src/ tests/ scripts/
```

## Tests

Every `tests/test_*.py` file carries one level, `unit`, `integration` or `e2e`, and any
number of tags. [tests/CLAUDE.md](tests/CLAUDE.md) holds the rules for choosing them and
the conventions for writing a test, so read it before you add a test file.
`tests/test_level_markers.py` rejects a file whose marks break those rules.

## Commits

Commit subjects follow [Conventional Commits](https://www.conventionalcommits.org/):
`<type>(<scope>): <description>`. The scope is optional, and a `!` before the colon marks
a breaking change. The allowed types are `feat`, `fix`, `docs`, `style`, `refactor`,
`perf`, `test`, `build`, `ci`, `chore` and `revert`. A subject is at most 72 characters.

Two hooks in `.githooks/` check commits, but git does not run them until you enable
them in your clone:

```sh
git config core.hooksPath .githooks
```

- `commit-msg` rejects a subject that breaks the rules above. It lets merge commits and
  `fixup!` or `squash!` commits through.
- `pre-commit` fails the commit when a staged file looks like a secret (it uses gitleaks
  when installed), is a private-key file, a `.env` file or a file named as a secret or
  credential, is larger than 500 KB, holds a merge-conflict marker or a no-commit marker
  (`NO_COMMIT_MARKERS` in the hook lists them), is a broken symlink, or is invalid JSON,
  YAML or TOML. It only warns about trailing whitespace, a missing final newline, mixed
  line endings, and a `pyproject.toml` staged without its lock file. Then it runs `ruff
  check` and `ruff format --check` over `src/` and `tests/`, and the full `uv run
  pytest`, so each commit takes as long as the suite.

## Pull requests and CI

`.github/workflows/ci.yml` runs on every pull request and on every push to `main`. On
Ubuntu it installs uv and bun, runs both `bun install`s and `uv sync --dev`, then
`ruff check` and `ruff format --check` over `src/` and `tests/`, then `uv run pytest`,
which there includes the Worker suites and the end-to-end suite. In CI a test whose tool
is missing fails instead of skipping. Run `scripts/check.sh` before you push, since it
covers all of this.

## Before you change code

- Read [docs/architecture.md](docs/architecture.md) before any non-trivial change, and
  update it in the same change that makes it stale.
- A change to a reader-facing page, such as the digest templates or `/settings`, follows
  [docs/design/ui-language.md](docs/design/ui-language.md).
- The coding conventions are in the [Conventions](CLAUDE.md#conventions) section of
  `CLAUDE.md`.
