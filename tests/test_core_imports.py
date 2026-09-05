"""The layering rules, in the only form that cannot go stale.

§1: the core (`service_layer` + `domain`) names Protocols and lets the
composition root supply bodies, so it imports neither `adapters` nor
`bootstrap`. `doctor` was the one module that broke this — it built Worker
sources and asked `bootstrap.build_store` what the wiring had resolved to. That
is not pipeline logic, it is a question *about* the wiring, so it moved into
`diagnostics/` rather than earning an exception here.

§2: `diagnostics/` may import anything below it; nothing below it may import
`diagnostics`. A layer only entrypoints reach is a layer that can be deleted
without touching a digest run.
"""

import ast
from pathlib import Path

SRC = Path("src/cyris")
CORE = (SRC / "service_layer", SRC / "domain")
NOT_IN_CORE = ("cyris.adapters", "cyris.bootstrap")
BELOW_DIAGNOSTICS = (
    SRC / "service_layer",
    SRC / "domain",
    SRC / "adapters",
    SRC / "bootstrap.py",
    SRC / "config.py",
)


def _runtime_imports(path: Path) -> list[tuple[int, str]]:
    """Every `from x import y` that actually runs — `if TYPE_CHECKING:` is a name."""
    tree = ast.parse(path.read_text())
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.dump(node.test):
            guarded.update(id(child) for child in ast.walk(node))
    return [
        (node.lineno, node.module)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and id(node) not in guarded
    ]


def _python_files(target: Path) -> list[Path]:
    return [target] if target.is_file() else sorted(target.rglob("*.py"))


def _offenders(targets, forbidden: tuple[str, ...]) -> list[str]:
    found = []
    for target in targets:
        for path in _python_files(target):
            for lineno, module in _runtime_imports(path):
                if module.startswith(forbidden):
                    found.append(f"{path}:{lineno} imports {module}")
    return found


def test_the_core_imports_no_adapter_and_no_composition_root() -> None:
    assert _offenders(CORE, NOT_IN_CORE) == []


def test_nothing_below_diagnostics_imports_diagnostics() -> None:
    assert _offenders(BELOW_DIAGNOSTICS, ("cyris.diagnostics",)) == []
