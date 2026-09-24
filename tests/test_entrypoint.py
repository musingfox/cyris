"""Tests for docker/entrypoint.sh role defaults."""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"

# The image's /bin/sh is dash, and macOS's sh is bash: signal handling differs between the
# two, so the SIGTERM behaviour is only evidence when dash itself runs the script.
DASH = shutil.which("dash")
needs_dash = pytest.mark.skipif(DASH is None, reason="dash is not installed")
SHELLS = ["sh", pytest.param("dash", marks=needs_dash)]


def _recorded_env(
    tmp_path: Path,
    *,
    role: str,
    stub: str,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    rec = tmp_path / "recorded.env"
    script = bin_dir / stub
    script.write_text(f"#!/bin/sh\nenv > '{rec}'\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    _stub(bin_dir, "python", "exit 0")

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["CYRIS_ROLE"] = role
    for name in (
        "CYRIS_STORE_BACKEND",
        "CYRIS_HTML_OUTPUT_ENABLED",
        "CYRIS_PROMOTE_PUBLISH_ENABLED",
    ):
        env.pop(name, None)
    if extra_env:
        env.update(extra_env)

    subprocess.run(["sh", str(ENTRYPOINT)], env=env, check=True, cwd=tmp_path)
    recorded: dict[str, str] = {}
    for line in rec.read_text().splitlines():
        key, _, value = line.partition("=")
        recorded[key] = value
    return recorded


def _stub(bin_dir: Path, name: str, body: str) -> None:
    script = bin_dir / name
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


def _stubbed_env(
    tmp_path: Path, role: str, python_body: str, cyris_body: str
) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.log"
    _stub(bin_dir, "python", f"echo python >> '{calls}'\n{python_body}")
    _stub(bin_dir, "cyris", f"echo \"cyris $1\" >> '{calls}'\n{cyris_body}")
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["CYRIS_ROLE"] = role
    return env, calls


def _calls(calls: Path) -> list[str]:
    return calls.read_text().splitlines() if calls.exists() else []


def _run_role(
    tmp_path: Path, role: str, python_body: str, cyris_body: str = "exit 0", shell: str = "sh"
) -> tuple[int, list[str]]:
    """Run the entrypoint with `python` and `cyris` stubbed; return exit code and call order."""
    env, calls = _stubbed_env(tmp_path, role, python_body, cyris_body)
    result = subprocess.run([shell, str(ENTRYPOINT)], env=env, cwd=tmp_path)
    return result.returncode, _calls(calls)


class _RunPass:
    """A `run` pass under dash, started in its own process group so a test can clean up
    whatever the pass leaves behind."""

    def __init__(self, tmp_path: Path, python_body: str, cyris_body: str) -> None:
        assert DASH is not None
        env, self.calls = _stubbed_env(tmp_path, "run", python_body, cyris_body)
        self.proc = subprocess.Popen(
            [DASH, str(ENTRYPOINT)], env=env, cwd=tmp_path, start_new_session=True
        )

    def wait_for_call(self, line: str, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while line not in _calls(self.calls):
            assert time.monotonic() < deadline, f"{line!r} never ran: {_calls(self.calls)}"
            time.sleep(0.02)

    def terminate(self, within: float) -> int:
        """SIGTERM the shell alone, as `Container.stop()` does; its exit code."""
        self.proc.send_signal(signal.SIGTERM)
        return self.proc.wait(timeout=within)

    def cleanup(self) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.proc.pid, signal.SIGKILL)
        self.proc.wait()


@pytest.fixture
def run_pass(tmp_path: Path):
    started: list[_RunPass] = []

    def start(python_body: str = "exit 0", cyris_body: str = "exit 0") -> _RunPass:
        started.append(_RunPass(tmp_path, python_body, cyris_body))
        return started[-1]

    yield start
    for p in started:
        p.cleanup()


class TestRunRoleProbesEgress:
    """Gemini and OpenAI refuse by egress location, and placement moves between runs."""

    @pytest.mark.parametrize("shell", SHELLS)
    def test_probe_runs_before_the_pipeline(self, tmp_path: Path, shell: str) -> None:
        code, calls = _run_role(tmp_path, "run", "exit 0", shell=shell)
        assert code == 0
        assert calls == ["python", "cyris run", "cyris promote-sync"]

    @pytest.mark.parametrize("shell", SHELLS)
    def test_a_failed_probe_does_not_stop_the_run(self, tmp_path: Path, shell: str) -> None:
        code, calls = _run_role(tmp_path, "run", "exit 1", shell=shell)
        assert code == 0
        assert calls == ["python", "cyris run", "cyris promote-sync"]

    @pytest.mark.parametrize("shell", SHELLS)
    def test_a_failed_run_still_syncs_votes_and_fails_the_pass(
        self, tmp_path: Path, shell: str
    ) -> None:
        # A run that stops on incomplete settings must not strand the votes.
        code, calls = _run_role(
            tmp_path, "run", "exit 0", 'test "$1" = run && exit 3; exit 0', shell=shell
        )
        assert calls == ["python", "cyris run", "cyris promote-sync"]
        assert code == 3

    def test_ui_role_does_not_probe(self, tmp_path: Path) -> None:
        # `exec` hands the process to the stub, so the ui role records one call.
        _, calls = _run_role(tmp_path, "ui", "exit 0")
        assert calls == ["cyris triage-ui"]


@needs_dash
class TestRunRoleStopsOnSigterm:
    """`Container.stop()` is one SIGTERM to the shell, which is PID 1 in the `run` role."""

    def test_sigterm_during_the_run_ends_the_pass_with_143(self, run_pass) -> None:
        p = run_pass(cyris_body='test "$1" = run && exec sleep 30; exit 0')
        p.wait_for_call("cyris run")
        assert p.terminate(within=3) == 143
        time.sleep(0.5)
        assert _calls(p.calls) == ["python", "cyris run"]

    def test_sigterm_during_the_probe_ends_the_pass_before_the_run(self, run_pass) -> None:
        p = run_pass(python_body="exec sleep 2")
        p.wait_for_call("python")
        assert p.terminate(within=4) == 143
        assert _calls(p.calls) == ["python"]

    def test_sigterm_reaches_the_running_run(self, run_pass, tmp_path: Path) -> None:
        pidfile = tmp_path / "step.pid"
        p = run_pass(cyris_body=f"test \"$1\" = run && echo $$ > '{pidfile}' && exec sleep 30")
        p.wait_for_call("cyris run")
        p.terminate(within=5)
        with pytest.raises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)

    def test_sigterm_reaches_the_running_promote_sync(self, run_pass, tmp_path: Path) -> None:
        pidfile = tmp_path / "step.pid"
        p = run_pass(
            cyris_body=f"test \"$1\" = promote-sync && echo $$ > '{pidfile}' && exec sleep 30\n"
            "exit 0"
        )
        p.wait_for_call("cyris promote-sync")
        p.terminate(within=5)
        with pytest.raises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)


class TestContainerRoleDefaultsToD1Store:
    def test_run_defaults_store_to_d1(self, tmp_path: Path) -> None:
        recorded = _recorded_env(tmp_path, role="run", stub="cyris")
        assert recorded["CYRIS_STORE_BACKEND"] == "d1"

    def test_run_keeps_preset_json_store(self, tmp_path: Path) -> None:
        recorded = _recorded_env(
            tmp_path, role="run", stub="cyris", extra_env={"CYRIS_STORE_BACKEND": "json"}
        )
        assert recorded["CYRIS_STORE_BACKEND"] == "json"

    def test_ui_defaults_store_to_d1(self, tmp_path: Path) -> None:
        recorded = _recorded_env(tmp_path, role="ui", stub="cyris")
        assert recorded["CYRIS_STORE_BACKEND"] == "d1"

    def test_cron_does_not_set_store_backend(self, tmp_path: Path) -> None:
        recorded = _recorded_env(tmp_path, role="cron", stub="supercronic")
        assert "CYRIS_STORE_BACKEND" not in recorded


class TestContainerRoleDefaultsToPublishing:
    def test_run_defaults_publish_flags_on(self, tmp_path: Path) -> None:
        recorded = _recorded_env(tmp_path, role="run", stub="cyris")
        assert recorded["CYRIS_HTML_OUTPUT_ENABLED"] == "true"
        assert recorded["CYRIS_PROMOTE_PUBLISH_ENABLED"] == "true"

    def test_run_keeps_preset_publish_disabled(self, tmp_path: Path) -> None:
        recorded = _recorded_env(
            tmp_path,
            role="run",
            stub="cyris",
            extra_env={"CYRIS_PROMOTE_PUBLISH_ENABLED": "false"},
        )
        assert recorded["CYRIS_PROMOTE_PUBLISH_ENABLED"] == "false"

    def test_ui_does_not_set_publish_flags(self, tmp_path: Path) -> None:
        recorded = _recorded_env(tmp_path, role="ui", stub="cyris")
        assert "CYRIS_HTML_OUTPUT_ENABLED" not in recorded
        assert "CYRIS_PROMOTE_PUBLISH_ENABLED" not in recorded
