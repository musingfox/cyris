"""A sqlite install whose file cannot be opened: each command says so on one ✗ line.

`cyris doctor`, `cyris run` and `cyris triage-ui` load the config through
`load_effective_config`, which opens the file and applies the schema before anything
else. A file that is not a database, a directory where it should be, or a file where
its directory should be must end that load as a `D1Error` the command reports, never
as a traceback.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from cyris.entrypoints.cli import app

pytestmark = pytest.mark.integration

runner = CliRunner()


def _not_a_database(root: Path) -> Path:
    path = root / "state" / "cyris.sqlite3"
    path.parent.mkdir()
    path.write_text("this text is not a SQLite database " * 20)
    return path


def _a_directory(root: Path) -> Path:
    path = root / "state" / "cyris.sqlite3"
    path.mkdir(parents=True)
    return path


def _a_file_for_its_directory(root: Path) -> Path:
    (root / "state").write_text("a file where the directory should be")
    return root / "state" / "cyris.sqlite3"


BROKEN = [_not_a_database, _a_directory, _a_file_for_its_directory]


def _install(root: Path, path: Path) -> Path:
    config = root / "cyris.toml"
    config.write_text(f'[store]\nbackend = "sqlite"\nsqlite_path = "{path}"\n')
    return config


@pytest.mark.parametrize("broken", BROKEN, ids=lambda f: f.__name__.strip("_"))
@pytest.mark.parametrize(
    "command", [["doctor"], ["run", "--period", "morning"], ["triage-ui"]], ids=lambda c: c[0]
)
def test_an_unopenable_file_ends_the_command_on_one_line(tmp_path, monkeypatch, broken, command):
    monkeypatch.chdir(tmp_path)
    path = broken(tmp_path)
    config = _install(tmp_path, path)

    result = runner.invoke(
        app, [*command, "--config", str(config), "--sources", str(tmp_path / "sources.yaml")]
    )

    assert result.exit_code == 1, result.output
    # Exit 1 through typer.Exit, not an exception escaping the command.
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    [line] = [line for line in result.output.splitlines() if line.startswith("✗")]
    assert str(path) in line


@pytest.mark.parametrize("command", [["doctor"], ["run", "--period", "morning"], ["triage-ui"]])
def test_the_hint_names_the_sqlite_file_not_cloudflare(tmp_path, monkeypatch, command):
    """A local install has no Cloudflare keys to check; its fix is the file."""
    monkeypatch.chdir(tmp_path)
    path = _not_a_database(tmp_path)
    config = _install(tmp_path, path)

    result = runner.invoke(
        app, [*command, "--config", str(config), "--sources", str(tmp_path / "sources.yaml")]
    )

    assert "✗ sqlite — " in result.output
    assert "[store] sqlite_path" in result.output
    for cloudflare in ("database_id", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"):
        assert cloudflare not in result.output


def test_a_config_elsewhere_opens_the_file_beside_it_not_in_the_working_directory(
    tmp_path, monkeypatch
):
    install = tmp_path / "inst"
    install.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    config = install / "cyris.toml"
    config.write_text('[store]\nbackend = "sqlite"\n')

    result = runner.invoke(
        app, ["ingest", "--config", str(config), "--sources", str(install / "sources.yaml")]
    )

    # The settings are not there yet, so the command stops; it has opened the store by then.
    assert (install / "state" / "cyris.sqlite3").is_file(), result.output
    assert not (elsewhere / "state").exists()
