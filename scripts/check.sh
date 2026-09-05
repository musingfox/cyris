#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
bun install --frozen-lockfile
(cd workers/rss && bun install --frozen-lockfile)
bunx vitest run
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pytest
