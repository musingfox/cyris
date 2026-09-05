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


def _runtime_imports(source: str) -> list[tuple[int, str]]:
    """Every module named by an import that actually runs.

    Both forms count. `import cyris.adapters.store` is an `ast.Import`, which an
    earlier version of this file did not collect at all — the whole rule was
    one keyword away from being unenforced. `if TYPE_CHECKING:` is a name, not
    a dependency, so it stays out.
    """
    tree = ast.parse(source)
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.dump(node.test):
            guarded.update(id(child) for child in ast.walk(node))

    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        if isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
    return found


def _python_files(target: Path) -> list[Path]:
    return [target] if target.is_file() else sorted(target.rglob("*.py"))


def _offenders(targets, forbidden: tuple[str, ...]) -> list[str]:
    found = []
    for target in targets:
        for path in _python_files(target):
            for lineno, module in _runtime_imports(path.read_text()):
                if module.startswith(forbidden):
                    found.append(f"{path}:{lineno} imports {module}")
    return found


def test_the_core_imports_no_adapter_and_no_composition_root() -> None:
    assert _offenders(CORE, NOT_IN_CORE) == []


def test_nothing_below_diagnostics_imports_diagnostics() -> None:
    assert _offenders(BELOW_DIAGNOSTICS, ("cyris.diagnostics",)) == []


def test_the_plain_import_form_is_not_a_way_through() -> None:
    source = "import cyris.adapters.store.d1_store\nimport cyris.bootstrap\n"

    named = [module for _, module in _runtime_imports(source)]

    assert named == ["cyris.adapters.store.d1_store", "cyris.bootstrap"]


def test_a_type_checking_import_is_not_an_offence() -> None:
    source = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from cyris.bootstrap import Deps\n"
    )

    assert "cyris.bootstrap" not in [module for _, module in _runtime_imports(source)]
