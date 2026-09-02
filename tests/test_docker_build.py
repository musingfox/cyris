"""Dockerfile COPY operands must be git-tracked so a clone can build."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

EXPECTED_COPY_OPERANDS = {
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "LICENSE",
    "src",
    "docker/crontab",
    "docker/entrypoint.sh",
    "sources.example.yaml",
}


def parse_copy_operands(dockerfile: str) -> set[str]:
    operands: set[str] = set()
    for raw in dockerfile.splitlines():
        line = raw.strip()
        if not line.startswith("COPY "):
            continue
        tokens = [t for t in line.split()[1:] if not t.startswith("--")]
        if len(tokens) < 2:
            continue
        operands.update(tokens[:-1])
    return operands


def untracked_copy_operands(operands: set[str], *, cwd: Path) -> list[str]:
    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        pytest.skip("not a git working directory")
    untracked: list[str] = []
    for src in sorted(operands):
        check = subprocess.run(
            ["git", "ls-files", "--error-unmatch", src],
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        if check.returncode != 0:
            untracked.append(src)
    return untracked


def test_dockerfile_copy_operands_are_tracked():
    operands = parse_copy_operands((REPO / "Dockerfile").read_text())
    assert operands == EXPECTED_COPY_OPERANDS
    assert untracked_copy_operands(operands, cwd=REPO) == []


def test_helper_reports_gitignored_cyris_toml():
    operands = parse_copy_operands("COPY cyris.toml /app/\n")
    assert untracked_copy_operands(operands, cwd=REPO) == ["cyris.toml"]


def test_helper_skips_outside_git(tmp_path):
    with pytest.raises(pytest.skip.Exception):
        untracked_copy_operands({"src"}, cwd=tmp_path)
