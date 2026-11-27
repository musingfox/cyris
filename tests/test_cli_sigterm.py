"""How `cyris run` and `cyris triage-ui` answer the SIGTERM that stops a Container.

In the Container the run role is PID 1's child under `docker/entrypoint.sh`; a
stop is a SIGTERM. Python's default for it kills the process outright, so
`run_digest`'s own `finally` — the one that records the run — never runs.
"""

import signal
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from cyris.entrypoints.cli import app

pytestmark = pytest.mark.integration

runner = CliRunner()


def _loaded_config() -> MagicMock:
    """A config past every gate `run` passes before the pipeline.

    A mock answers each gate with a truthy mock, and an awaiting-onboarding
    answer would end the run before `run_digest` is reached.
    """
    cfg = MagicMock()
    cfg.awaiting_onboarding.return_value = False
    return cfg


def _invoke_run_recording_sigterm_handler() -> tuple[object, list[object]]:
    recorded: list[object] = []

    async def fake_run_digest(deps: object, options: object) -> SimpleNamespace:
        recorded.append(signal.getsignal(signal.SIGTERM))
        return SimpleNamespace(rendered="")

    with (
        patch("cyris.bootstrap.load_effective_config", MagicMock(return_value=_loaded_config())),
        patch("cyris.bootstrap.build_deps", MagicMock()),
        patch("cyris.service_layer.run_digest.run_digest", fake_run_digest),
    ):
        result = runner.invoke(app, ["run"])
    return result, recorded


def test_run_enters_the_pipeline_with_sigterm_handled() -> None:
    result, recorded = _invoke_run_recording_sigterm_handler()

    assert result.exit_code == 0, result.output
    [handler] = recorded
    assert handler is not signal.SIG_DFL


def test_run_puts_the_previous_sigterm_handler_back() -> None:
    before = signal.getsignal(signal.SIGTERM)

    result, [during] = _invoke_run_recording_sigterm_handler()

    assert result.exit_code == 0, result.output
    assert during != before
    assert signal.getsignal(signal.SIGTERM) == before


_CHILD = """
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


async def fake_run_digest(deps, options):
    print("started", flush=True)
    try:
        {body}
    finally:
        {cleanup}
        print("finally ran", flush=True)
    return SimpleNamespace(rendered="")


config = MagicMock()
config.awaiting_onboarding.return_value = False

with (
    patch("cyris.bootstrap.load_effective_config", MagicMock(return_value=config)),
    patch("cyris.bootstrap.build_deps", MagicMock()),
    patch("cyris.service_layer.run_digest.run_digest", fake_run_digest),
):
    from cyris.entrypoints.cli import app

    app(["run"])
"""


def _sigterm_run(body: str, cleanup: str = "pass") -> tuple[int, str]:
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD.format(body=body, cleanup=cleanup)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "started"
        child.send_signal(signal.SIGTERM)
        stdout, _ = child.communicate(timeout=10)
        return child.returncode, stdout
    finally:
        child.kill()
        child.wait()


def test_sigterm_during_a_blocking_call_lets_it_finish_then_exits_143() -> None:
    # A D1 write is a sync call; tearing it midway could leave half its chunks written.
    returncode, stdout = _sigterm_run(
        'time.sleep(1); print("call finished", flush=True); await asyncio.sleep(30)'
    )

    assert returncode == 143
    assert "call finished" in stdout
    assert "finally ran" in stdout


def test_sigterm_while_awaiting_runs_cleanup_and_exits_143() -> None:
    returncode, stdout = _sigterm_run("await asyncio.sleep(30)")

    assert returncode == 143
    assert "finally ran" in stdout


def test_sigterm_during_cleanup_lets_the_cleanup_finish() -> None:
    # The cleanup is where the run is recorded. Nothing awaits after it, so the
    # cancellation has nowhere to land and the run ends with its own status.
    returncode, stdout = _sigterm_run("pass", cleanup="time.sleep(1)")

    assert "finally ran" in stdout
    assert returncode == 0


_UI_CHILD = """
import asyncio
from unittest.mock import MagicMock, patch


class FakeServer:
    def __init__(self, **kwargs):
        pass

    async def start(self):
        # Printed once triage-ui first awaits, which is after its signal
        # handlers are in place; a SIGTERM before that kills it outright.
        asyncio.get_running_loop().call_soon(lambda: print("started", flush=True))

    async def stop(self):
        pass


with (
    patch("cyris.bootstrap.load_effective_config", MagicMock()),
    patch("cyris.bootstrap.build_d1_client", MagicMock(return_value=None)),
    patch("cyris.bootstrap.build_settings", MagicMock()),
    patch("cyris.entrypoints.triage_server.TriageServer", FakeServer),
):
    from cyris.entrypoints.cli import app

    app(["triage-ui"])
"""


def test_triage_ui_logs_the_sigterm_and_the_finished_shutdown() -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", _UI_CHILD],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # A child that never prints "started" would block the read below forever;
    # killing it ends stdout, and the exit code assertion fails instead.
    watchdog = threading.Timer(10, child.kill)
    watchdog.start()
    try:
        assert child.stdout is not None
        for line in child.stdout:
            if line.strip() == "started":
                break
        child.send_signal(signal.SIGTERM)
        _, stderr = child.communicate(timeout=10)
    finally:
        watchdog.cancel()
        child.kill()
        child.wait()

    assert child.returncode == 0
    assert "triage-ui: SIGTERM, stopping" in stderr
    assert "triage-ui: stopped" in stderr
