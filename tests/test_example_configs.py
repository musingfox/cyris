"""A copy of `cyris.toml.example` is a first boot, and `settings.example.toml` holds the rest.

The two together hold what the one example used to: a SQLite install reads from its file
only the tables that are not grade D, and doctor fails a file that sets one. The grade-D
keys wait in a file that nothing reads but `cyris settings push`.
"""

import shutil
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cyris.config import _SETTINGS_TABLES, GRADE_D_KEYS, REQUIRED_KEYS
from cyris.entrypoints.cli import app

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "cyris.toml.example"
SETTINGS = ROOT / "settings.example.toml"
runner = CliRunner()


def _doc(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def copied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`cp cyris.toml.example cyris.toml` in a directory holding nothing else."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CYRIS_STORE_SQLITE_PATH", raising=False)
    return Path(shutil.copy(EXAMPLE, tmp_path / "cyris.toml"))


def test_the_example_names_sqlite() -> None:
    assert _doc(EXAMPLE)["store"]["backend"] == "sqlite"


def test_the_example_holds_no_settings_table_and_no_tracked_topics() -> None:
    assert set(_doc(EXAMPLE)) == {
        "agent_vault",
        "store",
        "html_output",
        "promote",
        "newsletter",
        "rss",
    }


def test_the_example_no_longer_claims_its_keys_must_stay() -> None:
    assert "must stay present" not in EXAMPLE.read_text(encoding="utf-8")


def test_the_settings_file_holds_every_required_key_and_nothing_else() -> None:
    doc = _doc(SETTINGS)

    assert set(doc) == set(_SETTINGS_TABLES)
    keys = {f"{table}.{field}" for table, body in doc.items() for field in body}
    assert keys == set(REQUIRED_KEYS)
    assert keys <= set(GRADE_D_KEYS)
    assert len(keys) == 25


def test_the_settings_file_says_it_is_only_a_source_for_the_push() -> None:
    header = SETTINGS.read_text(encoding="utf-8").split("[general]")[0]

    assert "only the source for `cyris settings push`" in header


def test_a_copy_of_the_example_is_a_first_boot_to_doctor(copied: Path) -> None:
    result = runner.invoke(app, ["doctor", "--config", str(copied)])
    lines = result.output.splitlines()

    assert any(line.startswith("✓ file settings —") for line in lines), result.output
    assert any(line.startswith("✓ config file —") for line in lines), result.output
    assert any(line.startswith("✓ build —") for line in lines), result.output
    assert any(line.startswith("✓ digest output —") for line in lines), result.output
    assert any(line.startswith("! sources —") for line in lines), result.output
    assert any("article store (sqlite) — not created yet" in line for line in lines), result.output
    # The one failure of a first boot is the settings the wizard is about to save (ADR-0003).
    failed = [line.split(" — ")[0] for line in lines if line.startswith("✗")]
    assert failed == ["✗ settings"], result.output
    assert "/onboarding" in result.output


def test_doctor_and_config_show_leave_a_copy_of_the_example_alone(copied: Path) -> None:
    runner.invoke(app, ["doctor", "--config", str(copied)])
    runner.invoke(app, ["config", "show", "--config", str(copied)])

    assert list(copied.parent.iterdir()) == [copied]


def test_a_settings_file_copy_pushes_to_d1_and_creates_nothing_here(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, d1
) -> None:
    """As the trial wizard runs it: a scratch directory, and no line naming a backend."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CYRIS_STORE_BACKEND")
    monkeypatch.delenv("CYRIS_STORE_SQLITE_PATH", raising=False)
    settings = Path(shutil.copy(SETTINGS, tmp_path / "settings.toml"))

    result = runner.invoke(app, ["settings", "push", "--config", str(settings)])

    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == "D1 database db"
    assert list(tmp_path.iterdir()) == [settings]
    rows = {row["key"] for row in d1.query("SELECT key FROM settings").rows}
    assert rows == set(REQUIRED_KEYS)
