"""Drive the Worker vitest suite when the JS toolchain is installed."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VITEST = ROOT / "node_modules/.bin/vitest"


def test_worker_vitest_suite():
    if not VITEST.exists():
        pytest.skip("bun install at the repo root (vitest binary missing)")
    result = subprocess.run(
        [str(VITEST), "run"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
