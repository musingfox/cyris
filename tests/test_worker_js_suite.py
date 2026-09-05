"""Drive the Workers' JS suites when the toolchain is installed.

There are two of them and they run on different runners: `workers/app/` under
vitest from the repo root (its config lives beside `wrangler.toml`, which is
also at the root because the image is built from the whole repo), and
`workers/rss/` under `node --test` from its own package. `promote` and
`newsletter` have no tests at all.

Neither ran in CI until 2026-09-05, and the failure was silent both times: a
suite that skips reports the same green as a suite that passes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VITEST = ROOT / "node_modules/.bin/vitest"
RSS_WORKER = ROOT / "workers/rss"


def _skip_unless_ci(reason: str) -> None:
    """Skipping is right on a machine without the toolchain, and wrong in CI."""
    assert not os.environ.get("CI"), f"CI must provide the JS toolchain: {reason}"
    pytest.skip(reason)


def test_app_worker_vitest_suite():
    if not VITEST.exists():
        _skip_unless_ci("bun install at the repo root (vitest binary missing)")
    result = subprocess.run([str(VITEST), "run"], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + "\n" + result.stderr


def test_rss_worker_node_suite():
    """`workers/rss/` is outside the vitest include, so it needs its own run.

    Its feed parser is what fills the buffer every hour; breaking it used to
    leave CI green.
    """
    if shutil.which("node") is None:
        _skip_unless_ci("node is not on PATH")
    if not (RSS_WORKER / "node_modules").exists():
        # Its own package, its own install: `src/parse.js` imports
        # `fast-xml-parser`, which the root install does not provide.
        _skip_unless_ci("bun install in workers/rss (node_modules missing)")

    # The package script passes a glob for the shell to expand; naming the files
    # keeps that out of `subprocess`, and an empty list would run zero tests and
    # report success.
    suites = sorted(p.name for p in (RSS_WORKER / "test").glob("*.test.js"))
    assert suites, "no test files under workers/rss/test/"

    result = subprocess.run(
        ["node", "--test", *(f"test/{name}" for name in suites)],
        cwd=RSS_WORKER,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
