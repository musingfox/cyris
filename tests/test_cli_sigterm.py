"""How `cyris run` answers the SIGTERM the Container runtime sends to stop it.

In the Container the run role is PID 1's child under `docker/entrypoint.sh`; a
stop is a SIGTERM. Python's default for it kills the process outright, so
`run_digest`'s own `finally` — the one that records the run — never runs.
"""

import signal
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from cyris.entrypoints.cli import app

runner = CliRunner()


def _invoke_run_recording_sigterm_handler() -> tuple[object, list[object]]:
    recorded: list[object] = []

    async def fake_run_digest(deps: object, options: object) -> SimpleNamespace:
        recorded.append(signal.getsignal(signal.SIGTERM))
        return SimpleNamespace(rendered="")

    with (
        patch("cyris.bootstrap.load_effective_config", MagicMock()),
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

    _invoke_run_recording_sigterm_handler()

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


with (
    patch("cyris.bootstrap.load_effective_config", MagicMock()),
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
