"""Every test module carries one level mark and only registered tags."""

import ast
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.guard]

_LEVELS = ("unit", "integration", "e2e")
_TAGS = frozenset({"guard", "js", "real_fixture"})
_SELECTABLE = frozenset(_LEVELS) | _TAGS


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
        names.extend(name for name, node in _outer_marks(value) if not isinstance(node, ast.Call))
    return names


def filename_level_violations(filename: str, source: str) -> list[str]:
    tokens = set(re.split(r"[^a-z0-9]+", Path(filename).name.lower()))
    carried = set(_pytestmark_names(source))
    return [
        f"{filename} names {level} but its pytestmark does not carry {level}"
        for level in _LEVELS
        if level in tokens and level not in carried
    ]


def level_violations(source: str) -> list[str]:
    levels = [name for name in _pytestmark_names(source) if name in _LEVELS]
    if not levels:
        return ["no level mark on the module pytestmark"]
    if len(levels) > 1:
        return [f"two levels on the module pytestmark: {', '.join(levels)}"]
    return []


def stray_mark_violations(source: str) -> list[str]:
    tree = ast.parse(source)
    allowed: set[int] = set()
    for value in _pytestmark_values(tree):
        allowed.update(id(node) for node in ast.walk(value))
    problems = []
    for name, node in _outer_marks(tree):
        if name not in _SELECTABLE or id(node) in allowed:
            continue
        problems.append(f"line {node.lineno} applies {name} outside the module pytestmark")
    return problems


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


def test_one_level_mark_is_accepted() -> None:
    source = "import pytest\npytestmark = pytest.mark.unit\ndef test_a(): pass\n"

    assert level_violations(source) == []


def test_one_level_plus_a_tag_is_accepted() -> None:
    source = "import pytest\npytestmark = [pytest.mark.integration, pytest.mark.js]\n"

    assert level_violations(source) == []


def test_a_module_without_a_level_is_rejected() -> None:
    source = "import pytest\ndef test_a(): pass\n"

    violations = level_violations(source)

    assert len(violations) == 1
    assert "no level" in violations[0]


def test_two_level_marks_are_rejected() -> None:
    source = "import pytest\npytestmark = [pytest.mark.unit, pytest.mark.integration]\n"

    violations = level_violations(source)

    assert len(violations) == 1
    assert "two levels" in violations[0]


def test_a_level_decorator_is_rejected() -> None:
    source = (
        "import pytest\n"
        "pytestmark = pytest.mark.unit\n"
        "@pytest.mark.integration\n"
        "def test_a(): pass\n"
    )

    violations = stray_mark_violations(source)

    assert len(violations) == 1
    assert "line 3" in violations[0]


def test_a_class_level_mark_is_rejected() -> None:
    source = (
        "import pytest\n"
        "pytestmark = pytest.mark.unit\n"
        "class TestX:\n"
        "    pytestmark = pytest.mark.e2e\n"
    )

    violations = stray_mark_violations(source)

    assert len(violations) == 1
    assert "line 4" in violations[0]


def test_a_builtin_mark_bound_to_a_name_is_accepted() -> None:
    source = (
        "import pytest\npytestmark = pytest.mark.unit\ndash = pytest.mark.usefixtures('dash')\n"
    )

    assert stray_mark_violations(source) == []


def unregistered_mark_violations(source: str) -> list[str]:
    return [
        f"unregistered mark {name}" for name in _pytestmark_names(source) if name not in _SELECTABLE
    ]


def test_a_slow_mark_is_rejected() -> None:
    source = "import pytest\npytestmark = [pytest.mark.unit, pytest.mark.slow]\n"

    violations = unregistered_mark_violations(source)

    assert len(violations) == 1
    assert "slow" in violations[0]


def test_a_tooling_mark_is_rejected() -> None:
    source = "import pytest\npytestmark = [pytest.mark.unit, pytest.mark.tooling]\n"

    violations = unregistered_mark_violations(source)

    assert len(violations) == 1
    assert "tooling" in violations[0]


def test_a_call_form_mark_in_pytestmark_is_ignored() -> None:
    source = (
        "import pytest\npytestmark = [pytest.mark.unit, pytest.mark.skipif(True, reason='x')]\n"
    )

    assert level_violations(source) == []
    assert unregistered_mark_violations(source) == []


def test_registered_tags_are_accepted() -> None:
    source = (
        "import pytest\n"
        "pytestmark = [pytest.mark.unit, pytest.mark.guard, pytest.mark.real_fixture]\n"
    )

    assert unregistered_mark_violations(source) == []


def registration_problems(ini: dict) -> list[str]:
    problems: list[str] = []
    if ini.get("strict_markers") is not True:
        problems.append("strict_markers is not enabled")
    declared = [entry.split(":", 1)[0].strip() for entry in ini.get("markers") or []]
    for name in (*_LEVELS, *sorted(_TAGS)):
        if name not in declared:
            problems.append(f"missing marker {name}")
    for name in declared:
        if name not in _SELECTABLE:
            problems.append(f"unexpected marker {name}")
    return problems


def _pytest_ini() -> dict:
    data = tomllib.loads(Path("pyproject.toml").read_text())
    return data["tool"]["pytest"]["ini_options"]


def test_the_registered_markers_match_the_six_names() -> None:
    assert registration_problems(_pytest_ini()) == []


def test_a_missing_e2e_marker_is_named() -> None:
    ini = _pytest_ini()
    ini["markers"] = [entry for entry in ini["markers"] if not entry.startswith("e2e:")]

    problems = registration_problems(ini)

    assert len(problems) == 1
    assert "e2e" in problems[0]


def test_an_extra_slow_marker_is_named() -> None:
    ini = _pytest_ini()
    ini["markers"] = [*ini["markers"], "slow: slow tests"]

    problems = registration_problems(ini)

    assert len(problems) == 1
    assert "slow" in problems[0]


def test_strict_markers_must_stay_enabled() -> None:
    ini = _pytest_ini()
    del ini["strict_markers"]

    problems = registration_problems(ini)

    assert len(problems) == 1
    assert "strict_markers" in problems[0]


def test_every_test_module_obeys_the_marker_rules() -> None:
    files = sorted(Path("tests").glob("test_*.py"))
    assert len(files) >= 88

    violations: list[str] = []
    unit_files = 0
    integration_files = 0
    for path in files:
        source = path.read_text()
        names = set(_pytestmark_names(source))
        if "unit" in names:
            unit_files += 1
        if "integration" in names:
            integration_files += 1
        for problem in (
            *level_violations(source),
            *stray_mark_violations(source),
            *unregistered_mark_violations(source),
            *filename_level_violations(path.name, source),
        ):
            violations.append(f"{path.name}: {problem}")

    assert violations == []
    assert unit_files >= 1
    assert integration_files >= 1
