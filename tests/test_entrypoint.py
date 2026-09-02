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
