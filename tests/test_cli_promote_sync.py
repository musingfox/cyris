"""What the hourly tick does when no vote Worker is deployed.

`docker/entrypoint.sh` runs `promote-sync` as the last command of the `run` role,
so its exit code is the container's. The vote Worker is optional — a deployment
without one must not report every tick as a failure.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from cyris.entrypoints.cli import app

runner = CliRunner()


@pytest.fixture
def config_without_promote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    monkeypatch.delenv("CYRIS_PROMOTE_WORKER_URL", raising=False)
    monkeypatch.delenv("CYRIS_PROMOTE_TOKEN", raising=False)

    config_path = tmp_path / "cyris.toml"
    config_path.write_text(
        f"""
[general]
timezone = "Asia/Taipei"

[agent_vault]
path = "{tmp_path / "agent-vault"}"
"""
    )
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text("sources: []")
    (tmp_path / "agent-vault").mkdir()

    return config_path, sources_path


def test_no_vote_worker_is_a_shape_not_a_failure(
    config_without_promote: tuple[Path, Path],
) -> None:
    config_path, sources_path = config_without_promote

    result = runner.invoke(
        app,
        ["promote-sync", "--config", str(config_path), "--sources", str(sources_path)],
    )

    assert result.exit_code == 0
    assert "not configured" in result.output
