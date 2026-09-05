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


def _writes_to_disk(path: Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text())):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in WRITERS
        ):
            return True
    return False


def test_only_the_documented_fallbacks_write_to_local_disk() -> None:
    writers = {
        str(path.relative_to(SRC)) for path in sorted(SRC.rglob("*.py")) if _writes_to_disk(path)
    }

    assert writers == ALLOWED


def test_the_comparisons_write_nothing() -> None:
    """`embed-compare` and `llm-compare` emit; where the output lands is the shell's call.

    They used to append a JSONL and fill `agent-vault/llm-compare/` on their own,
    which put a diagnostic tool's scratch output in the same place as the article
    store — the confusion §4 is kept to prevent.
    """
    assert "diagnostics/compare.py" not in ALLOWED
    assert not _writes_to_disk(SRC / "diagnostics/compare.py")

    cli = (SRC / "entrypoints/cli.py").read_text()
    assert "llm-compare" not in cli or 'agent_vault.path / "llm-compare"' not in cli
    assert '"--log"' not in cli


def test_architecture_says_what_the_residency_table_covers() -> None:
    architecture = Path("docs/architecture.md").read_text()

    assert "tests/test_local_writes.py" in architecture.split("## 5.")[0]
