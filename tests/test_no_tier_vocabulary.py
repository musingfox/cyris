"""No tier vocabulary is left outside the history that names the rename."""

import fnmatch
import re
import subprocess
from pathlib import Path

import pytest

from cyris.domain.models import LEGACY_LANE_NAMES

pytestmark = [pytest.mark.unit, pytest.mark.guard]

REPO = Path(__file__).resolve().parent.parent

# The old concept: a Tier (also inside an identifier such as SummarizeTier), the source_tier
# column, or a tier: key. Lowercase prose ("free tier", "storage tier") is another meaning of
# the word and is not matched.
_OLD_VOCABULARY = re.compile(r"Tier|\bsource_tier\b|\btier:")
_LEGACY_MARKER = "# legacy-tier"
_ARCHITECTURE = "docs/architecture.md"
_HISTORY_SECTION = "## 7"

# Path globs that keep the old words, each with the reason it may. Python files can also keep
# a single line with the _LEGACY_MARKER comment; architecture.md keeps only its §7.
_ALLOWED: tuple[tuple[str, str], ...] = (
    ("docs/milestones/*.md", "milestones narrate the rename as it happened"),
    ("docs/decisions/*.md", "ADRs record the decisions as they were taken"),
    ("docs/spec/audits/*.md", "the 2026-10-08 audit names the tests it read"),
    ("CHANGELOG.md", "release history; [Unreleased] names the rename once"),
    ("src/cyris/adapters/store/migrations.py", "the migration that renames tier to lane"),
    ("tests/legacy_schema.py", "the schema before the rename, frozen for migration tests"),
    ("tests/test_legacy_tier.py", "tests the legacy spellings still load"),
    ("tests/test_schema_migrations.py", "migration tests seed the old column and keys"),
    ("tests/test_no_tier_vocabulary.py", "this guard; its patterns are the words it looks for"),
)


def _offenders(root: Path, relpaths: list[str]) -> list[str]:
    found: list[str] = []
    for rel in relpaths:
        if any(fnmatch.fnmatchcase(rel, glob) for glob, _ in _ALLOWED):
            continue
        raw = (root / rel).read_bytes()
        if b"\0" in raw:
            continue
        lines = raw.decode("utf-8", errors="replace").splitlines()
        in_history = False
        for number, line in enumerate(lines, start=1):
            if rel == _ARCHITECTURE and line.startswith("## "):
                in_history = line.startswith(_HISTORY_SECTION)
            if in_history or not _OLD_VOCABULARY.search(line):
                continue
            if rel.endswith(".py") and _LEGACY_MARKER in line:
                continue
            found.append(f"{rel}:{number}")
    return found


def _tracked_files() -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    return [rel for rel in listed.split("\0") if rel]


def test_the_repo_has_no_tier_vocabulary_outside_history() -> None:
    assert _offenders(REPO, _tracked_files()) == []


def test_a_source_tier_outside_the_allowlist_is_flagged(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.md").write_text("ok\nthe source_tier column\n")
    assert _offenders(tmp_path, ["docs/note.md"]) == ["docs/note.md:2"]


def test_unrelated_tier_prose_is_not_flagged() -> None:
    unrelated = [
        "docs/hosting-and-cost.md",
        "src/cyris/adapters/embedding.py",
        "src/cyris/adapters/output/pages_deploy.py",
    ]
    assert "free tier" in (REPO / unrelated[0]).read_text()
    assert _offenders(REPO, unrelated) == []


def test_changelog_unreleased_records_the_rename_and_value_map() -> None:
    text = (REPO / "CHANGELOG.md").read_text()
    unreleased = text.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]
    assert "`tier:`" in unreleased and "`lane:`" in unreleased
    for old, new in LEGACY_LANE_NAMES.items():
        assert f"`{old}`" in unreleased and f"`{new.value}`" in unreleased
