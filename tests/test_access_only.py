"""The retired sign-in names survive only in the records of the past."""

import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.guard]

ROOT = Path(__file__).resolve().parents[1]

_NAMES = ("CYRIS_UI_TOKEN", "CYRIS_UI_ACCESS_HOST", "cyris_session")
_SELF = "tests/test_access_only.py"
_RECORDS = (
    "CHANGELOG.md",
    "docs/decisions/0009-",
    "docs/decisions/0010-",
    "docs/decisions/0012-",
    "docs/decisions/0015-",
    "docs/decisions/0021-",
    "docs/milestones/",
    "docs/spec/audits/",
)


def leftover_names(root: Path) -> list[str]:
    """Only tracked files count: an untracked or gitignored file is not part of the repo."""
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, text=True, check=True
    ).stdout
    hits: list[str] = []
    for rel in filter(None, listed.split("\0")):
        if rel == _SELF or rel.startswith(_RECORDS):
            continue
        path = root / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if any(name in line for name in _NAMES):
                hits.append(f"{rel}:{number}")
    return sorted(hits)


def _track_all(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)


def test_the_repo_carries_no_retired_sign_in_name():
    assert leftover_names(ROOT) == []


def test_a_retired_name_is_a_hit_outside_the_records(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.py").write_text(f"x = '{_NAMES[0]}'\n")
    (tmp_path / "CHANGELOG.md").write_text(f"{_NAMES[0]}\n")
    (tmp_path / "docs/milestones").mkdir(parents=True)
    (tmp_path / "docs/milestones/x.md").write_text(f"{_NAMES[0]}\n")
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe" + _NAMES[0].encode())
    _track_all(tmp_path)
    assert leftover_names(tmp_path) == ["src/a.py:1"]
    (tmp_path / "src/a.py").unlink()
    assert leftover_names(tmp_path) == []


def test_a_worker_source_hit_names_its_file(tmp_path: Path):
    (tmp_path / "workers/app/src").mkdir(parents=True)
    (tmp_path / "workers/app/src/router.js").write_text(f"const c = '{_NAMES[2]}';\n")
    _track_all(tmp_path)
    assert leftover_names(tmp_path) == ["workers/app/src/router.js:1"]


def test_an_untracked_or_ignored_file_naming_a_retired_name_is_not_a_hit(tmp_path: Path):
    (tmp_path / ".gitignore").write_text(".env\n")
    (tmp_path / "tracked.py").write_text(f"x = '{_NAMES[0]}'\n")
    (tmp_path / "untracked.py").write_text(f"x = '{_NAMES[0]}'\n")
    (tmp_path / ".env").write_text(f"{_NAMES[0]}=1\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "tracked.py", ".gitignore"], cwd=tmp_path, check=True)
    assert leftover_names(tmp_path) == ["tracked.py:1"]
