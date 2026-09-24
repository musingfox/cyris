"""How `cyris run` answers the SIGTERM the Container runtime sends to stop it.

In the Container the run role is PID 1's child under `docker/entrypoint.sh`; a
stop is a SIGTERM. Python's default for it kills the process outright, so
`run_digest`'s own `finally` — the one that records the run — never runs.
"""

import signal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from cyris.entrypoints.cli import app

runner = CliRunner()


def _invoke_run_recording_sigterm_handler() -> tuple[object, list[object]]:
    recorded: list[object] = []

    def fake_asyncio_run(coro: object) -> SimpleNamespace:
        recorded.append(signal.getsignal(signal.SIGTERM))
        coro.close()  # type: ignore[attr-defined]
        return SimpleNamespace(rendered="")

    with (
        patch("cyris.bootstrap.load_effective_config", MagicMock()),
        patch("cyris.bootstrap.build_deps", MagicMock()),
        patch("cyris.entrypoints.cli.asyncio.run", fake_asyncio_run),
    ):
        result = runner.invoke(app, ["run"])
    return result, recorded


def test_run_enters_the_pipeline_with_a_sigterm_handler_that_exits_143() -> None:
    result, recorded = _invoke_run_recording_sigterm_handler()

    assert result.exit_code == 0, result.output
    [handler] = recorded
    assert handler is not signal.SIG_DFL
    assert callable(handler)
    try:
        handler(signal.SIGTERM, None)
    except SystemExit as exit_:
        assert exit_.code == 143
    else:
        raise AssertionError("the SIGTERM handler did not raise SystemExit")


def test_run_puts_the_previous_sigterm_handler_back() -> None:
    before = signal.getsignal(signal.SIGTERM)

    _invoke_run_recording_sigterm_handler()

    assert signal.getsignal(signal.SIGTERM) == before
