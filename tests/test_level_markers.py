"""Unregistered marks fail collection, and the suite stays on pytest 9.0.2."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

_BASE = "915b0e0d56cce5273c1da839b9e22981197f96af"


def test_strict_markers_is_enabled(request: pytest.FixtureRequest) -> None:
    assert request.config.getini("strict_markers") is True


def _project(tmp_path: Path, source: str) -> None:
    (tmp_path / "pyproject.toml").write_text(Path("pyproject.toml").read_text())
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text(source)


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )


def test_an_unregistered_mark_fails_collection(tmp_path: Path) -> None:
    _project(
        tmp_path,
        "import pytest\npytestmark = pytest.mark.bogus\n\ndef test_x():\n    pass\n",
    )

    result = _run(tmp_path)
    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert "'bogus' not found in `markers`" in output
    rootdir = next(line for line in output.splitlines() if line.startswith("rootdir:"))
    assert Path(rootdir.split(":", 1)[1].strip()) == tmp_path


def test_a_registered_level_mark_collects(tmp_path: Path) -> None:
    _project(
        tmp_path,
        "import pytest\npytestmark = pytest.mark.unit\n\ndef test_x():\n    pass\n",
    )

    result = _run(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr


def test_pytest_stays_locked_at_9_0_2() -> None:
    version = subprocess.check_output(["uv", "run", "pytest", "--version"], text=True)
    assert "pytest 9.0.2" in version

    diff = subprocess.check_output(["git", "diff", _BASE, "--", "uv.lock"], text=True)
    changed = [
        line
        for line in diff.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    assert changed == [
        '-    { name = "pytest", specifier = ">=8.0" },',
        '+    { name = "pytest", specifier = ">=9" },',
    ]
