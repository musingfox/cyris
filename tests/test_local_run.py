"""POST /run on a local install: the app Worker's manual run, answered by `cyris triage-ui`.

The wizard's last step posts `/run?period=&date=`, which only the app Worker had
(`workers/app/src/router.js`). A local server answers it with the same refusals and
the same answer, and starts `cyris run` as the Worker starts its run container.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiohttp.test_utils import make_mocked_request

from cyris.entrypoints import cli
from cyris.entrypoints.triage_server import TriageServer

pytestmark = pytest.mark.unit

VALUES = {"general.timezone": "Asia/Taipei", "general.digest_schedule": ["08:00", "20:00"]}
# 2026-11-07 09:30 in Taipei; its yesterday is 2026-11-06.
NOW = datetime(2026, 11, 7, 1, 30, 15, 250000, tzinfo=UTC)


def _server(*, values: dict | None = None, behind_app_worker: bool = False):
    started: list[tuple[str | None, str | None]] = []
    server = TriageServer(
        values=VALUES if values is None else values,
        clock=lambda: NOW,
        behind_app_worker=behind_app_worker,
        start_run=lambda period, date: started.append((period, date)),
    )
    return server, started


async def _post(server: TriageServer, query: str) -> tuple[int, dict]:
    response = await server._handle_post_run(make_mocked_request("POST", f"/run?{query}"))
    return response.status, json.loads(response.text)


def _routes(server: TriageServer) -> set[tuple[str, str]]:
    return {
        (route.method, route.resource.canonical)
        for route in server._app.router.routes()
        if route.resource is not None
    }


@pytest.mark.parametrize(
    ("query", "started"),
    [
        ("period=morning&date=2026-11-07", ("morning", "2026-11-07")),
        ("period=evening&date=2026-11-06", ("evening", "2026-11-06")),
        ("period=evening", ("evening", None)),
        ("", (None, None)),
    ],
)
async def test_a_run_starts_and_answers_as_the_worker_does(query, started) -> None:
    server, runs = _server()

    status, body = await _post(server, query)

    assert status == 200
    # workers/app/src/index.js startRun: `{ started: "run", at: new Date().toISOString() }`.
    assert body == {"started": "run", "at": "2026-11-07T01:30:15.250Z"}
    assert runs == [started]


@pytest.mark.parametrize(
    ("query", "status", "error"),
    [
        ("date=2026-11-07", 400, "date needs a period"),
        ("period=noon", 400, "period must be one of morning, evening"),
        ("period=noon&date=2026-11-07", 400, "period must be one of morning, evening"),
        ("period=morning&date=2026-11-31", 400, "date must be a real YYYY-MM-DD date"),
        ("period=morning&date=20261107", 400, "date must be a real YYYY-MM-DD date"),
        ("period=morning&date=", 400, "date must be a real YYYY-MM-DD date"),
        (
            "period=morning&date=2026-11-05",
            400,
            "date must be today or yesterday in Asia/Taipei (2026-11-06 or 2026-11-07)",
        ),
        (
            "period=morning&date=2026-11-08",
            400,
            "date must be today or yesterday in Asia/Taipei (2026-11-06 or 2026-11-07)",
        ),
    ],
)
async def test_the_worker_refusals_start_nothing(query, status, error) -> None:
    """router.js:275-290 and refuseRunDate, word for word."""
    server, runs = _server()

    assert await _post(server, query) == (status, {"error": error})
    assert runs == []


@pytest.mark.parametrize("timezone", [None, "", "Mars/Olympus"])
async def test_a_date_with_no_readable_time_zone_is_refused_with_503(timezone) -> None:
    values = {} if timezone is None else {"general.timezone": timezone}
    server, runs = _server(values=values)

    assert await _post(server, "period=morning&date=2026-11-07") == (
        503,
        {"error": "could not read the deployment's time zone to check the date; try again"},
    )
    assert runs == []


async def test_the_date_is_checked_in_the_time_zone_saved_since_the_server_started() -> None:
    """The wizard saves the zone a step before it runs; the server reads what it holds now."""
    server, runs = _server(values={})
    server._values["general.timezone"] = "America/Los_Angeles"

    # 2026-11-07 01:30 UTC is still 2026-11-06 in Los Angeles.
    status, _ = await _post(server, "period=morning&date=2026-11-05")

    assert status == 200
    assert runs == [("morning", "2026-11-05")]


async def test_a_run_that_cannot_start_answers_500() -> None:
    def refuse(period, date):
        raise FileNotFoundError("no such interpreter")

    server = TriageServer(values=VALUES, clock=lambda: NOW, start_run=refuse)

    status, body = await _post(server, "period=morning")

    assert status == 500
    assert "no such interpreter" in body["error"]


def test_the_route_exists_only_where_no_app_worker_answers_it() -> None:
    local, _ = _server()
    behind, _ = _server(behind_app_worker=True)

    assert ("POST", "/run") in _routes(local)
    assert ("POST", "/run") not in _routes(behind)
    assert ("POST", "/run") not in _routes(TriageServer(values=VALUES))


@pytest.mark.parametrize(
    ("period", "date", "args"),
    [
        ("evening", "2026-11-06", ["--period", "evening", "--date", "2026-11-06"]),
        ("morning", None, ["--period", "morning"]),
        (None, None, ["--if-due"]),
    ],
)
def test_the_run_is_the_container_entrypoints_command_on_this_servers_config(
    monkeypatch, period, date, args
) -> None:
    """docker/entrypoint.sh's `run` role, with the server's own --config and --sources."""
    calls = []

    class Run:
        pid = 1

        def __init__(self, argv, **kwargs) -> None:
            calls.append((argv, kwargs))

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(subprocess, "Popen", Run)

    cli._start_detached_run(Path("conf/cyris.toml"), Path("conf/sources.yaml"), period, date)

    [(argv, kwargs)] = calls
    assert argv == [
        sys.executable, "-m", "cyris", "run", *args,
        "--config", "conf/cyris.toml", "--sources", "conf/sources.yaml",
    ]  # fmt: skip
    # Its own session: a Ctrl+C on the server's terminal does not stop a run half way.
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_a_finished_run_is_reaped_without_holding_up_the_answer(monkeypatch) -> None:
    """A run nobody waits on stays a zombie until the next Popen; the start must not wait."""
    finish = threading.Event()
    reaped = threading.Event()

    class Run:
        pid = 1

        def __init__(self, argv, **kwargs) -> None:
            pass

        def wait(self) -> int:
            finish.wait(5)
            reaped.set()
            return 0

    monkeypatch.setattr(subprocess, "Popen", Run)

    cli._start_detached_run(Path("cyris.toml"), Path("sources.yaml"), "morning", None)

    # Returned while the run is still going, and nothing has reaped it yet.
    assert not reaped.is_set()
    finish.set()
    assert reaped.wait(5)


def test_a_real_child_leaves_no_zombie(monkeypatch) -> None:
    """With a real process: once it exits, nothing is left for the server to collect."""
    true = shutil.which("true")
    if true is None:
        pytest.skip("no `true` to run in place of cyris")
    started = []
    real_popen = subprocess.Popen

    def popen(argv, **kwargs):
        started.append(real_popen([true], **kwargs))
        return started[-1]

    monkeypatch.setattr(subprocess, "Popen", popen)

    cli._start_detached_run(Path("cyris.toml"), Path("sources.yaml"), "morning", None)

    [run] = started
    deadline = time.monotonic() + 5
    while run.returncode is None and time.monotonic() < deadline:
        time.sleep(0.05)
    # The reaper's wait collected it, so the kernel holds no exit status for it.
    assert run.returncode == 0
    with pytest.raises(ChildProcessError):
        os.waitpid(run.pid, os.WNOHANG)
