"""`cyris triage-ui` serves `/settings` and nothing else.

Plain `def` tests: the command calls `asyncio.run()`, which refuses to start
inside the loop pytest-asyncio would give an `async def` test.
"""

import asyncio
import signal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cyris.entrypoints.cli import app

runner = CliRunner()


class FakeServer:
    """Records how the command built it, then stops the command at once."""

    calls: list[tuple[tuple, dict]] = []

    def __init__(self, *args, **kwargs) -> None:
        FakeServer.calls.append((args, kwargs))

    async def start(self) -> None:
        asyncio.get_running_loop().call_soon(signal.raise_signal, signal.SIGINT)

    async def stop(self) -> None:
        pass


@pytest.fixture
def config(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    config_path = tmp_path / "cyris.toml"
    config_path.write_text(
        f"""
[general]
timezone = "Asia/Taipei"

[llm_provider]
api_key = "test"
model = "test"

[agent_vault]
path = "{tmp_path / "agent-vault"}"
"""
    )
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text("sources: []")
    (tmp_path / "agent-vault").mkdir()

    FakeServer.calls = []
    monkeypatch.setattr("cyris.entrypoints.triage_server.TriageServer", FakeServer)
    return config_path, sources_path


def _run(config: tuple[Path, Path]):
    config_path, sources_path = config
    return runner.invoke(
        app, ["triage-ui", "--config", str(config_path), "--sources", str(sources_path)]
    )


def test_startup_names_only_the_settings_url(config: tuple[Path, Path]) -> None:
    result = _run(config)
    assert result.exit_code == 0, result.output
    assert "Settings:  http://127.0.0.1:8766/settings" in result.output
    assert "Triage UI:" not in result.output


def test_the_ui_role_opens_no_article_store(config: tuple[Path, Path], monkeypatch) -> None:
    def refuse(*args, **kwargs):
        raise AssertionError("ui role opened an article store")

    monkeypatch.setattr("cyris.bootstrap.build_store", refuse)
    result = _run(config)
    assert result.exit_code == 0, result.output
    [(args, kwargs)] = FakeServer.calls
    assert args == ()
    assert "sources_origin" not in kwargs
    assert kwargs["source_store"] is None


def test_an_empty_d1_starts_the_page_with_no_values(config: tuple[Path, Path], monkeypatch) -> None:
    """The page gets what the home holds and nothing more: an empty D1 holds nothing."""
    from fakes import SqliteD1

    db = SqliteD1()
    monkeypatch.setattr("cyris.adapters.store.d1.D1Client", lambda **_kw: db)
    monkeypatch.setenv("CYRIS_STORE_BACKEND", "d1")
    monkeypatch.setenv("CYRIS_STORE_DATABASE_ID", "db")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")

    result = _run(config)

    assert result.exit_code == 0, result.output
    [(_args, kwargs)] = FakeServer.calls
    assert kwargs["values"] == {}
    assert kwargs["settings"] is not None


def test_help_describes_the_settings_server() -> None:
    result = runner.invoke(app, ["triage-ui", "--help"])
    assert result.exit_code == 0
    assert "article classification" not in result.output
