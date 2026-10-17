"""`cyris triage-ui` serves `/settings` and `/labels`.

Plain `def` tests: the command calls `asyncio.run()`, which refuses to start
inside the loop pytest-asyncio would give an `async def` test.
"""

import asyncio
import signal
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from fakes import seed_d1_settings
from typer.testing import CliRunner

from cyris.entrypoints.cli import app
from cyris.entrypoints.triage_server import TriageServer

pytestmark = pytest.mark.integration

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


def test_startup_names_the_settings_and_labels_urls(config: tuple[Path, Path]) -> None:
    result = _run(config)
    assert result.exit_code == 0, result.output
    assert "Settings:  http://127.0.0.1:8766/settings" in result.output
    assert "Labels:    http://127.0.0.1:8766/labels" in result.output
    assert "Triage UI:" not in result.output


def test_a_json_ui_role_opens_no_article_store(config: tuple[Path, Path], monkeypatch) -> None:
    def refuse(*args, **kwargs):
        raise AssertionError("ui role opened an article store")

    monkeypatch.setattr("cyris.bootstrap.build_store", refuse)
    result = _run(config)
    assert result.exit_code == 0, result.output
    [(args, kwargs)] = FakeServer.calls
    assert args == ()
    assert "sources_origin" not in kwargs
    assert kwargs["source_store"] is None
    assert kwargs["feed_health"] is None
    assert kwargs["article_store"] is None
    assert kwargs["blind_labels"] is None


def test_an_empty_d1_starts_the_page_with_only_the_defaulted_effort(
    config: tuple[Path, Path], monkeypatch
) -> None:
    """The page gets what the home holds and what runs on a default; an empty D1 has the latter."""
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
    assert kwargs["values"] == {"llm_provider.reasoning_effort": "default"}
    assert kwargs["settings"] is not None
    assert kwargs["feed_health"].read() == {}
    # /labels writes its answers to the articles as votes, so a D1 ui role opens both.
    assert kwargs["blind_labels"].progress() == (0, 0)
    assert kwargs["article_store"].count_by_state() == {}


def _page_settings(values: dict) -> dict:
    """GET /api/settings from the real server (FakeServer replaces the module's name)."""

    async def get() -> dict:
        client = TestClient(TestServer(TriageServer(values=values)._app))
        await client.start_server()
        try:
            return await (await client.get("/api/settings")).json()
        finally:
            await client.close()

    return asyncio.run(get())


def test_an_absent_effort_shows_on_the_page_as_default_not_missing(
    config: tuple[Path, Path], d1
) -> None:
    seed_d1_settings(d1, omit=["llm_provider.reasoning_effort"])

    result = _run(config)

    assert result.exit_code == 0, result.output
    [(_args, kwargs)] = FakeServer.calls
    assert kwargs["values"]["llm_provider.reasoning_effort"] == "default"
    data = _page_settings(kwargs["values"])
    assert "llm_provider.reasoning_effort" not in data["missing"]
    assert data["values"]["llm_provider.reasoning_effort"] == "default"


def test_an_invalid_stored_effort_is_left_out_of_the_page_values(
    config: tuple[Path, Path], d1
) -> None:
    seed_d1_settings(d1, **{"llm_provider.reasoning_effort": "xhigh"})

    result = _run(config)

    assert result.exit_code == 0, result.output
    [(_args, kwargs)] = FakeServer.calls
    assert "llm_provider.reasoning_effort" not in kwargs["values"]


def test_a_stored_effort_reaches_the_page_as_itself(config: tuple[Path, Path], d1) -> None:
    seed_d1_settings(d1, **{"llm_provider.reasoning_effort": "high"})

    result = _run(config)

    assert result.exit_code == 0, result.output
    [(_args, kwargs)] = FakeServer.calls
    assert kwargs["values"]["llm_provider.reasoning_effort"] == "high"


def test_help_describes_the_settings_server() -> None:
    result = runner.invoke(app, ["triage-ui", "--help"])
    assert result.exit_code == 0
    assert "article classification" not in result.output
