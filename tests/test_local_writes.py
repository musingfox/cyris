"""Every place cyris writes to local disk, and nothing else.

§4 lists where persistent state lives; the failure it exists to prevent is a
feature quietly finding a new home for state. A new `write_text` is how that
starts, so the set of files allowed to make one is pinned here rather than
rediscovered by the next audit.

The four that remain are all the same shape: a backend the cloud path does not
use. Under `[store] backend = "d1"` none of them runs.
"""

import ast
from pathlib import Path

SRC = Path("src/cyris")
WRITERS = {"write_text", "write_bytes", "mkdir", "touch", "unlink"}

ALLOWED = {
    "adapters/output/html_digest.py",  # agent-vault/html/, the no-D1 fallback
    "adapters/output/usage_log.py",  # usage.jsonl, the no-D1 fallback
    "adapters/store/article_store.py",  # the json store backend
    "diagnostics/doctor.py",  # the vault writability probe, skipped under D1
}


def _opened_for_writing(node: ast.Call) -> bool:
    """`open(p, "a")` and `p.open("w")` — the form that used to slip past.

    `usage_log.py` is the proof it did: it is caught by its `mkdir` on the line
    above, and its actual `open(log_path, "a")` was invisible. A new file that
    only opens and writes would have been listed by neither this set nor §4.
    """
    if isinstance(node.func, ast.Attribute) and node.func.attr == "open":
        positional = node.args[0] if node.args else None  # Path.open(mode)
    elif isinstance(node.func, ast.Name) and node.func.id == "open":
        positional = node.args[1] if len(node.args) > 1 else None  # open(path, mode)
    else:
        return False

    mode = next((kw.value for kw in node.keywords if kw.arg == "mode"), positional)
    if mode is None:
        return False  # no mode is read mode
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return any(flag in mode.value for flag in "wax+")
    return True  # a computed mode could be either; assume the worse one


def _writes_to_disk(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr in WRITERS:
            return True
        if _opened_for_writing(node):
            return True
    return False


def test_only_the_documented_fallbacks_write_to_local_disk() -> None:
    writers = {
        str(path.relative_to(SRC))
        for path in sorted(SRC.rglob("*.py"))
        if _writes_to_disk(path.read_text())
    }

    assert writers == ALLOWED


def test_the_comparisons_write_nothing() -> None:
    """`embed-compare` and `llm-compare` emit; where the output lands is the shell's call.

    They used to append a JSONL and fill `agent-vault/llm-compare/` on their own,
    which put a diagnostic tool's scratch output in the same place as the article
    store — the confusion §4 is kept to prevent.
    """
    assert "diagnostics/compare.py" not in ALLOWED
    assert not _writes_to_disk((SRC / "diagnostics/compare.py").read_text())

    cli = (SRC / "entrypoints/cli.py").read_text()
    assert "llm-compare" not in cli or 'agent_vault.path / "llm-compare"' not in cli
    assert '"--log"' not in cli


def test_a_plain_open_is_not_a_way_through() -> None:
    """No `mkdir` beside it, and the old guard saw nothing."""
    assert _writes_to_disk('with open(path, "a") as f:\n    f.write("x")\n')
    assert _writes_to_disk('path.open("w").write("x")\n')
    assert _writes_to_disk("with open(path, mode) as f:\n    f.write('x')\n")


def test_reading_a_file_is_not_a_write() -> None:
    assert not _writes_to_disk('with open(config_path, "rb") as f:\n    tomllib.load(f)\n')
    assert not _writes_to_disk("with open(sources_path) as f:\n    yaml.safe_load(f)\n")


def test_architecture_says_what_the_residency_table_covers() -> None:
    architecture = Path("docs/architecture.md").read_text()

    assert "tests/test_local_writes.py" in architecture.split("## 5.")[0]
