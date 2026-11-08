"""A sqlite install whose file cannot be opened: each command says so on one ✗ line.

`cyris doctor`, `cyris run` and `cyris triage-ui` load the config through
`load_effective_config`, which opens the file and applies the schema before anything
else. A file that is not a database, a directory where it should be, or a file where
its directory should be must end that load as a `D1Error` the command reports, never
as a traceback.
"""

import re
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


def _everything_in(root: Path) -> list[Path]:
    return sorted(root.rglob("*"))


@pytest.fixture
def first_boot(tmp_path, monkeypatch) -> Path:
    """An install directory holding its cyris.toml and nothing else, as the working directory."""
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "cyris.toml"
    config.write_text('[store]\nbackend = "sqlite"\n')
    return config


@pytest.mark.parametrize("command", [["doctor"], ["config", "show"]], ids=lambda c: c[0])
def test_a_reporting_command_creates_nothing_and_says_the_store_is_not_created(
    first_boot: Path, command: list[str]
) -> None:
    result = runner.invoke(app, [*command, "--config", str(first_boot)])

    assert _everything_in(first_boot.parent) == [first_boot], result.output
    assert "not created yet" in result.output
    assert str(first_boot.parent / "state" / "cyris.sqlite3") in result.output


def test_settings_push_creates_nothing_here_and_writes_to_d1(first_boot: Path, d1) -> None:
    first_boot.write_text('[store]\nbackend = "sqlite"\n\n[routing]\nscore_threshold = 70\n')

    result = runner.invoke(app, ["settings", "push", "--config", str(first_boot)])

    assert result.exit_code == 0, result.output
    assert _everything_in(first_boot.parent) == [first_boot]
    assert d1.query("SELECT key FROM settings").rows == [{"key": "routing.score_threshold"}]


def test_settings_push_does_not_open_a_file_that_exists(
    first_boot: Path, d1, monkeypatch: pytest.MonkeyPatch
) -> None:
    (first_boot.parent / "state").mkdir()
    (first_boot.parent / "state" / "cyris.sqlite3").write_text("not a database, and never read")

    def refuse(_path):
        raise AssertionError("settings push opened the local file")

    monkeypatch.setattr("cyris.adapters.store.local_d1.LocalD1Client", refuse)

    result = runner.invoke(app, ["settings", "push", "--config", str(first_boot)])

    assert result.exit_code == 0, result.output


def test_a_reporting_command_opens_a_file_that_exists(first_boot: Path) -> None:
    from cyris.adapters.store.d1 import apply_schema
    from cyris.adapters.store.local_d1 import LocalD1Client
    from cyris.adapters.store.settings import D1Settings

    client = LocalD1Client(first_boot.parent / "state" / "cyris.sqlite3")
    apply_schema(client)
    D1Settings(client).set({"digest.output_language": "de"})

    result = runner.invoke(app, ["config", "show", "--config", str(first_boot)])

    assert "not created yet" not in result.output
    assert re.search(r"digest\.output_language\s+\"de\"\s+SQLite settings", result.output)


def test_doctor_still_refuses_a_file_it_cannot_create(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    config = _install(tmp_path, locked / "state" / "cyris.sqlite3")

    result = runner.invoke(app, ["doctor", "--config", str(config)])

    locked.chmod(0o700)
    assert result.exit_code == 1, result.output
    assert "not created yet" not in result.output
    assert "[store] sqlite_path" in result.output


@pytest.mark.parametrize(
    "command", [["run", "--period", "morning"], ["ingest"]], ids=lambda c: c[0]
)
def test_a_run_still_creates_the_file_on_first_use(first_boot: Path, command: list[str]) -> None:
    result = runner.invoke(app, [*command, "--config", str(first_boot)])

    assert (first_boot.parent / "state" / "cyris.sqlite3").is_file(), result.output
