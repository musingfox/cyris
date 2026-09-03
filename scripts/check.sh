#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
cd workers/app
bun install --frozen-lockfile
bunx vitest run
cd ../..
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pytest
