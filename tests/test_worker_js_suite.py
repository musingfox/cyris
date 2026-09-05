"""Drive the Worker vitest suite when the JS toolchain is installed."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VITEST = ROOT / "node_modules/.bin/vitest"


def test_worker_vitest_suite():
    if not VITEST.exists():
        # A local run may not have the JS toolchain, and skipping is right there.
        # In CI it is not: the skip is silent, so between the workflow gaining
        # pytest and 2026-09-05 no Worker JS was ever run there.
        assert not os.environ.get("CI"), "CI must `bun install` before pytest"
        pytest.skip("bun install at the repo root (vitest binary missing)")
    result = subprocess.run(
        [str(VITEST), "run"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
