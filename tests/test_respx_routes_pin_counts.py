"""Every respx route a test registers has its exact call count asserted.

The respx half of docs/spec/tests-pin-every-outgoing-request.md. The bare `respx.mock`
every file here uses is respx's global router, built with `assert_all_called` off, so a
route nobody reads is never checked by respx either. `httpx.MockTransport` fakes are not
covered: their request lists take too many shapes for a scan without false positives.
"""

import ast
import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.guard]

TESTS = Path(__file__).parent
_VERBS = {"get", "post", "put", "patch", "delete", "head", "options", "route"}
_SWITCHED_OFF = re.compile(r"assert_all_(called|mocked)\s*=\s*False")


def _registers_a_route(node: ast.AST) -> bool:
    """`respx.<verb>(...)`, alone or chained, as in `respx.post(url).mock(...)`."""
    while isinstance(node, ast.Call | ast.Attribute):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in _VERBS
                and isinstance(func.value, ast.Name)
                and func.value.id == "respx"
            ):
                return True
            node = func
        else:
            node = node.value
    return False


def _on(node: ast.AST, name: str, attr: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == attr
        and isinstance(node.value, ast.Name)
        and node.value.id == name
    )


def _pins_its_count(scope: ast.AST, name: str) -> bool:
    """`name.call_count`, `len(name.calls)`, or `not name.called` (exactly zero)."""
    for node in ast.walk(scope):
        if _on(node, name, "call_count"):
            return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "len"
            and node.args
            and _on(node.args[0], name, "calls")
        ):
            return True
        if (
            isinstance(node, ast.UnaryOp)
            and isinstance(node.op, ast.Not)
            and _on(node.operand, name, "called")
        ):
            return True
    return False


def unpinned_routes(source: str) -> list[str]:
    """Each route registration in `source` whose call count no assertion can have read."""
    tree = ast.parse(source)
    scopes = [
        node
        for top in tree.body
        for node in ([top] if not isinstance(top, ast.ClassDef) else top.body)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    problems = []
    for stmt in ast.walk(tree):
        if not isinstance(stmt, ast.Expr | ast.Assign) or not _registers_a_route(stmt.value):
            continue
        scope = next((s for s in scopes if s.lineno <= stmt.lineno <= s.end_lineno), None)
        if scope is None:
            problems.append(f"line {stmt.lineno}: route registered outside a test")
        elif isinstance(stmt, ast.Expr):
            problems.append(f"line {stmt.lineno}: route not bound to a name")
        elif len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
            problems.append(f"line {stmt.lineno}: route not bound to a plain name")
        elif not _pins_its_count(scope, stmt.targets[0].id):
            problems.append(f"line {stmt.lineno}: {stmt.targets[0].id}.call_count never read")
    return problems


def switches_off_respx(source: str) -> bool:
    return bool(_SWITCHED_OFF.search(source))


def test_every_respx_route_has_its_call_count_asserted() -> None:
    problems = [
        f"{path.name} {problem}"
        for path in sorted(TESTS.glob("test_*.py"))
        for problem in unpinned_routes(path.read_text(encoding="utf-8"))
    ]

    assert problems == []


def test_no_test_switches_off_respxs_own_assertions() -> None:
    offenders = [
        str(path.relative_to(TESTS))
        for path in sorted(TESTS.rglob("*.py"))
        if switches_off_respx(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


_HEADER = "import respx\n\nURL = 'https://api.test/x'\n\n"


@pytest.mark.parametrize(
    ("body", "flagged"),
    [
        (
            "async def test_x():\n"
            "    async with respx.mock:\n"
            "        respx.post(URL).mock(return_value=None)\n",
            ["line 7: route not bound to a name"],
        ),
        (
            "def test_x():\n"
            "    route = respx.get(URL).mock(return_value=None)\n"
            "    assert route.called\n",
            ["line 6: route.call_count never read"],
        ),
        (
            "class TestX:\n"
            "    def test_x(self):\n"
            "        route = respx.get(URL).mock(return_value=None)\n"
            "        assert route.calls.last.request.method == 'GET'\n",
            ["line 7: route.call_count never read"],
        ),
        ("ROUTE = respx.get(URL)\n", ["line 5: route registered outside a test"]),
        (
            "def test_x():\n"
            "    route = respx.get(URL).mock(return_value=None)\n"
            "    assert route.call_count == 1\n",
            [],
        ),
        (
            "def test_x():\n"
            "    ack = respx.post(URL).mock(return_value=None)\n"
            "    assert not ack.called\n",
            [],
        ),
    ],
    ids=["bare", "called-only", "content-only", "module-level", "count", "zero"],
)
def test_the_scan_flags_a_planted_violation(body: str, flagged: list[str]) -> None:
    assert unpinned_routes(_HEADER + body) == flagged


def test_the_switch_off_scan_flags_a_planted_violation() -> None:
    assert switches_off_respx("with respx.mock(assert_all_called=" + "False):\n    pass\n")
    assert not switches_off_respx("with respx.mock:\n    pass\n")
