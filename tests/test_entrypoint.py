"""Tests for docker/entrypoint.sh role defaults."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"


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


def _run_role(
    tmp_path: Path, role: str, python_body: str, cyris_body: str = "exit 0"
) -> tuple[int, list[str]]:
    """Run the entrypoint with `python` and `cyris` stubbed; return exit code and call order."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.log"
    _stub(bin_dir, "python", f"echo python >> '{calls}'\n{python_body}")
    _stub(bin_dir, "cyris", f"echo \"cyris $1\" >> '{calls}'\n{cyris_body}")
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["CYRIS_ROLE"] = role
    result = subprocess.run(["sh", str(ENTRYPOINT)], env=env, cwd=tmp_path)
    lines = calls.read_text().splitlines() if calls.exists() else []
    return result.returncode, lines


class TestRunRoleProbesEgress:
    """Gemini and OpenAI refuse by egress location, and placement moves between runs."""

    def test_probe_runs_before_the_pipeline(self, tmp_path: Path) -> None:
        code, calls = _run_role(tmp_path, "run", "exit 0")
        assert code == 0
        assert calls == ["python", "cyris run", "cyris promote-sync"]

    def test_a_failed_probe_does_not_stop_the_run(self, tmp_path: Path) -> None:
        code, calls = _run_role(tmp_path, "run", "exit 1")
        assert code == 0
        assert calls == ["python", "cyris run", "cyris promote-sync"]

    def test_a_failed_run_still_syncs_votes_and_fails_the_pass(self, tmp_path: Path) -> None:
        # A run that stops on incomplete settings must not strand the votes.
        code, calls = _run_role(tmp_path, "run", "exit 0", 'test "$1" = run && exit 3; exit 0')
        assert calls == ["python", "cyris run", "cyris promote-sync"]
        assert code == 3

    def test_ui_role_does_not_probe(self, tmp_path: Path) -> None:
        # `exec` hands the process to the stub, so the ui role records one call.
        _, calls = _run_role(tmp_path, "ui", "exit 0")
        assert calls == ["cyris triage-ui"]


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
