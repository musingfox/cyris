"""Unregistered marks fail collection, and the suite stays on pytest 9.0.2."""

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.guard]

_BASE = "915b0e0d56cce5273c1da839b9e22981197f96af"
_LEVELS = ("unit", "integration", "e2e")


def _mark_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        node = node.func
    if not isinstance(node, ast.Attribute):
        return None
    mark = node.value
    if not isinstance(mark, ast.Attribute) or mark.attr != "mark":
        return None
    base = mark.value
    if not isinstance(base, ast.Name) or base.id != "pytest":
        return None
    return node.attr


def _outer_marks(tree: ast.AST) -> list[tuple[str, ast.AST]]:
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and _mark_name(node)]
    func_ids = {id(node.func) for node in calls}
    found: list[tuple[str, ast.AST]] = []
    for node in calls:
        name = _mark_name(node)
        if name is not None:
            found.append((name, node))
    for node in ast.walk(tree):
        if id(node) in func_ids or not isinstance(node, ast.Attribute):
            continue
        name = _mark_name(node)
        if name is not None:
            found.append((name, node))
    return found


def _pytestmark_values(tree: ast.Module) -> list[ast.AST]:
    values: list[ast.AST] = []
    for stmt in tree.body:
        value: ast.AST | None
        targets: list[ast.expr]
        if isinstance(stmt, ast.Assign):
            targets, value = list(stmt.targets), stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets, value = [stmt.target], stmt.value
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id == "pytestmark" for target in targets):
            values.append(value)
    return values


def _pytestmark_names(source: str) -> list[str]:
    tree = ast.parse(source)
    names: list[str] = []
    for value in _pytestmark_values(tree):
        names.extend(name for name, _ in _outer_marks(value))
    return names


def filename_level_violations(filename: str, source: str) -> list[str]:
    tokens = set(re.split(r"[^a-z0-9]+", Path(filename).name.lower()))
    carried = set(_pytestmark_names(source))
    return [
        f"{filename} names {level} but its pytestmark does not carry {level}"
        for level in _LEVELS
        if level in tokens and level not in carried
    ]


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


def test_a_file_named_e2e_but_marked_integration_is_rejected() -> None:
    source = "import pytest\npytestmark = pytest.mark.integration\n"

    violations = filename_level_violations("test_e2e_flows.py", source)

    assert len(violations) == 1
    assert "e2e" in violations[0]


def test_a_file_named_integration_but_marked_unit_is_rejected() -> None:
    source = "import pytest\npytestmark = pytest.mark.unit\n"

    violations = filename_level_violations("test_store_integration.py", source)

    assert len(violations) == 1
    assert "integration" in violations[0]


def test_a_name_without_a_level_word_is_accepted() -> None:
    source = "import pytest\npytestmark = pytest.mark.unit\n"

    assert filename_level_violations("test_unified_fetcher.py", source) == []


def test_a_name_that_carries_its_level_is_accepted() -> None:
    source = "import pytest\npytestmark = pytest.mark.e2e\n"

    assert filename_level_violations("test_e2e_release_smoke.py", source) == []


def test_the_renamed_modules_differ_only_by_their_mark() -> None:
    assert not Path("tests/test_e2e_flows.py").exists()
    assert not Path("tests/test_store_integration.py").exists()
    assert Path("tests/test_cli_flows.py").is_file()
    assert Path("tests/test_store_parity.py").is_file()

    diff = subprocess.check_output(
        [
            "git",
            "diff",
            "-M",
            _BASE,
            "--",
            "tests/test_e2e_flows.py",
            "tests/test_cli_flows.py",
            "tests/test_store_integration.py",
            "tests/test_store_parity.py",
        ],
        text=True,
    )
    assert "rename from tests/test_e2e_flows.py" in diff
    assert "rename to tests/test_cli_flows.py" in diff
    assert "rename from tests/test_store_integration.py" in diff
    assert "rename to tests/test_store_parity.py" in diff
    added = [
        line[1:]
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++") and line[1:].strip()
    ]
    removed = [
        line[1:]
        for line in diff.splitlines()
        if line.startswith("-") and not line.startswith("---") and line[1:].strip()
    ]
    assert removed == []
    assert added == [
        "pytestmark = pytest.mark.integration",
        "import pytest",
        "pytestmark = [pytest.mark.unit, pytest.mark.guard]",
    ]
