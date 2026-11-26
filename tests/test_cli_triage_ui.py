"""`cyris triage-ui` serves `/settings` and `/labels`.

Plain `def` tests: the command calls `asyncio.run()`, which refuses to start
inside the loop pytest-asyncio would give an `async def` test.
"""

import asyncio
import json
import os
import signal
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest
from aiohttp import web
from aiohttp.streams import StreamReader
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from fakes import seed_d1_settings
from typer.testing import CliRunner

from cyris.adapters.store.topics import D1TopicStore
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


def _built_behind_app_worker(config: tuple[Path, Path]) -> bool:
    result = _run(config)
    assert result.exit_code == 0, result.output
    [(_, kwargs)] = FakeServer.calls
    return kwargs["behind_app_worker"]


def test_a_command_not_told_otherwise_keeps_the_request_check(
    config: tuple[Path, Path], monkeypatch
) -> None:
    monkeypatch.delenv("CYRIS_BEHIND_APP_WORKER", raising=False)
    assert _built_behind_app_worker(config) is False


def test_the_app_workers_value_turns_the_request_check_off(
    config: tuple[Path, Path], monkeypatch
) -> None:
    monkeypatch.setenv("CYRIS_BEHIND_APP_WORKER", "true")
    assert _built_behind_app_worker(config) is True


@pytest.mark.parametrize("value", ["", "false", "0", "1", "TRUE", "yes", "true "])
def test_any_other_value_keeps_the_request_check(
    config: tuple[Path, Path], monkeypatch, value: str
) -> None:
    monkeypatch.setenv("CYRIS_BEHIND_APP_WORKER", value)
    assert _built_behind_app_worker(config) is False


def test_a_sqlite_ui_role_wires_every_store_and_builds_no_run_store(
    config: tuple[Path, Path], monkeypatch
) -> None:
    def refuse(*args, **kwargs):
        raise AssertionError("ui role built the run's article store")

    monkeypatch.setattr("cyris.bootstrap.build_store", refuse)
    result = _run(config)
    assert result.exit_code == 0, result.output
    [(args, kwargs)] = FakeServer.calls
    assert args == ()
    assert "sources_origin" not in kwargs
    assert kwargs["settings"] is not None
    assert kwargs["source_store"] is not None
    assert kwargs["feed_health"] is not None
    assert kwargs["topic_store"] is not None
    assert kwargs["blind_labels"] is not None
    assert kwargs["article_store"] is not None
    # A page vote writes its topic vote beside its stamp, on the one file (ADR-0044).
    assert isinstance(kwargs["issue_topics"], D1TopicStore)
    assert kwargs["issue_topics"]._db is kwargs["article_store"]._db
    assert kwargs["onboarding_articles"] is not None
    assert kwargs["feed_checker"] is not None
    assert kwargs["digest_store"] is not None
    assert kwargs["run_log"] is not None
    assert kwargs["interest_reader"] is not None
    assert kwargs["start_run"] is not None


def test_a_sqlite_ui_reads_the_feed_health_cyris_ingest_writes_to_the_same_file(
    config: tuple[Path, Path],
) -> None:
    """The Sources tab's reader is the local file's, so a poll `cyris ingest` recorded shows."""
    from cyris.adapters.store.d1 import apply_schema
    from cyris.adapters.store.feed_health import D1FeedHealthWriter
    from cyris.adapters.store.local_d1 import LocalD1Client
    from cyris.adapters.store.source_store import D1SourceStore
    from cyris.domain.models import SourceConfig

    result = _run(config)
    assert result.exit_code == 0, result.output
    [(_, kwargs)] = FakeServer.calls

    ingest_side = LocalD1Client(Path(os.environ["CYRIS_STORE_SQLITE_PATH"]))
    apply_schema(ingest_side)
    D1SourceStore(ingest_side).upsert(SourceConfig(name="Blog", url="https://blog.test/feed"))
    D1FeedHealthWriter(ingest_side).record(
        [("Blog", "HTTP 503")], datetime(2026, 11, 20, 1, tzinfo=UTC)
    )

    health = kwargs["feed_health"].read()["Blog"]
    assert (health.consecutive_failures, health.last_error, health.last_failed_at) == (
        1,
        "HTTP 503",
        "2026-11-20T01:00:00.000Z",
    )


def test_an_empty_d1_starts_the_page_with_only_the_optional_defaults(
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
    assert kwargs["values"] == {
        "llm_provider.reasoning_effort": "default",
        "notify.own_addresses": "",
    }
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


def test_a_d1_store_alone_keeps_the_request_check(
    config: tuple[Path, Path], d1, monkeypatch
) -> None:
    # The cloud's store is no proof of the cloud's Worker: a compose install can use D1 too.
    monkeypatch.delenv("CYRIS_BEHIND_APP_WORKER", raising=False)
    seed_d1_settings(d1)
    assert _built_behind_app_worker(config) is False


def test_help_describes_the_settings_server() -> None:
    result = runner.invoke(app, ["triage-ui", "--help"])
    assert result.exit_code == 0
    assert "article classification" not in result.output


def _with_html_output(config: tuple[Path, Path], enabled: bool) -> None:
    config_path, _ = config
    with config_path.open("a") as toml:
        toml.write(f'\n[html_output]\nenabled = {str(enabled).lower()}\noutput_dir = "site"\n')


def test_the_command_hands_the_server_its_pages_and_names_the_archive(config) -> None:
    _with_html_output(config, True)

    result = _run(config)

    assert result.exit_code == 0, result.output
    assert FakeServer.calls[0][1]["html_dir"] == Path("site")
    assert "Archive:   http://127.0.0.1:8766/" in result.output


def test_with_html_output_off_the_command_gives_the_server_no_pages(config) -> None:
    _with_html_output(config, False)

    _run(config)

    assert FakeServer.calls[0][1]["html_dir"] is None


class _LocalServer(TriageServer):
    """The real server as the command builds it, stopping the command once it has started."""

    built: list["_LocalServer"] = []

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        _LocalServer.built.append(self)

    async def start(self) -> None:
        asyncio.get_running_loop().call_soon(signal.raise_signal, signal.SIGINT)


def _vote_answers(config: tuple[Path, Path], monkeypatch) -> tuple[dict, int]:
    """The probe's answer and a page vote's status from the server the command builds."""
    _LocalServer.built = []
    monkeypatch.setattr("cyris.entrypoints.triage_server.TriageServer", _LocalServer)
    result = _run(config)
    assert result.exit_code == 0, result.output
    [server] = _LocalServer.built
    app = server._app
    app.freeze()

    async def send(method: str, body: bytes) -> web.StreamResponse:
        # Through the app's own dispatch, the Host check included, without a socket.
        payload = StreamReader(
            mock.Mock(_reading_paused=False), 2**16, loop=asyncio.get_running_loop()
        )
        payload.feed_data(body)
        payload.feed_eof()
        headers = {"Host": "127.0.0.1:8766", "Content-Type": "application/json"}
        request = make_mocked_request(
            method, "/api/vote", headers=headers, payload=payload, app=app
        )
        try:
            return await app._handle(request)
        except web.HTTPException as refusal:
            return refusal

    async def both() -> tuple[dict, int]:
        probe = await send("GET", b"")
        vote = await send(
            "POST", json.dumps({"url": "https://example.com/a", "vote": "up"}).encode()
        )
        return json.loads(probe.text), vote.status

    return asyncio.run(both())


def test_a_sqlite_server_takes_the_pages_votes(config: tuple[Path, Path], monkeypatch) -> None:
    config_path, _ = config
    with config_path.open("a") as toml:
        toml.write(
            '\n[store]\nbackend = "sqlite"\n'
            f'sqlite_path = "{config_path.parent / "state" / "cyris.sqlite3"}"\n'
        )

    # With the review each source holds, none on a new file (ADR-0050).
    assert _vote_answers(config, monkeypatch) == ({"authorized": True, "reviews": {}}, 200)


def test_a_d1_server_leaves_the_votes_to_the_clouds_queue(
    config: tuple[Path, Path], d1, monkeypatch
) -> None:
    # Not behind the app Worker, yet on production's D1: a vote here would block the loop on
    # D1's round trips, and the next promote-sync could stamp an older queued vote over it.
    monkeypatch.delenv("CYRIS_BEHIND_APP_WORKER", raising=False)
    seed_d1_settings(d1)

    assert _vote_answers(config, monkeypatch) == ({"authorized": True, "votes": False}, 405)


def _post_run_status(config: tuple[Path, Path], monkeypatch) -> tuple[int, list]:
    """The status `POST /run` gets from the server the command builds, and the runs it
    started. The run itself is recorded, never spawned."""
    config_path, _ = config
    with config_path.open("a") as toml:
        toml.write(
            '\n[store]\nbackend = "sqlite"\n'
            f'sqlite_path = "{config_path.parent / "state" / "cyris.sqlite3"}"\n'
        )
    started: list[tuple[str | None, str | None]] = []
    monkeypatch.setattr(
        "cyris.entrypoints.cli._start_detached_run",
        lambda _config, _sources, period, date: started.append((period, date)),
    )
    _LocalServer.built = []
    monkeypatch.setattr("cyris.entrypoints.triage_server.TriageServer", _LocalServer)

    result = _run(config)
    assert result.exit_code == 0, result.output
    [server] = _LocalServer.built

    async def post() -> int:
        # Resolved through the app's router, as a request is, without a socket.
        request = make_mocked_request("POST", "/run?period=morning", app=server._app)
        match = await server._app.router.resolve(request)
        if match.http_exception is not None:
            return match.http_exception.status
        return (await match.handler(request)).status

    return asyncio.run(post()), started


def test_a_sqlite_server_still_accepts_post_run(config: tuple[Path, Path], monkeypatch) -> None:
    status, started = _post_run_status(config, monkeypatch)

    assert status == 200
    assert started == [("morning", None)]
