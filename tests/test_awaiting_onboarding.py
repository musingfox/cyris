"""An empty source list is a deployment waiting for onboarding, not a failure.

A new deployment ships with no feed, so `cyris run` ends having fetched nothing
instead of exiting 1, a scheduled tick sends no "could not start" alert, and
`cyris doctor` reports the wait rather than a failed check.

Under pytest the CLI's `logging.basicConfig` finds pytest's handlers already on
the root logger, so the message a real process writes to stderr lands in `caplog`.
"""

import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fakes import SqliteD1, seed_d1_settings, settings_toml
from typer.testing import CliRunner

from cyris.domain.models import SourceConfig
from cyris.entrypoints.cli import app

pytestmark = pytest.mark.integration

runner = CliRunner()

WAITING = "waiting for onboarding"


@pytest.fixture(autouse=True)
def _info_logs(caplog: pytest.LogCaptureFixture) -> None:
    """The wait is an INFO line, which `caplog` would otherwise drop."""
    caplog.set_level(logging.INFO)


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Records every `build_deps` call: nothing is fetched without one."""
    calls: list[object] = []

    def fake_build_deps(cfg, *args, **kwargs):
        calls.append(cfg)
        raise RuntimeError("the run went past the sources gate")

    monkeypatch.setattr("cyris.bootstrap.build_deps", fake_build_deps)
    return calls


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records every "could not start" alert the scheduled run would send."""
    sent: list[str] = []
    monkeypatch.setattr(
        "cyris.entrypoints.cli._alert_could_not_start",
        lambda cfg, error: sent.append(str(error)),
    )
    return sent


def _at_hour(monkeypatch: pytest.MonkeyPatch, hour: int) -> None:
    monkeypatch.setattr(
        "cyris.utils.timezone.now_in_timezone",
        lambda tz: datetime(2026, 10, 20, hour, 5, tzinfo=ZoneInfo(tz)),
    )


def _d1_paths(tmp_path: Path) -> list[str]:
    """A D1 deployment whose two files hold nothing but the vault path."""
    config_path = tmp_path / "cyris.toml"
    config_path.write_text(f'[agent_vault]\npath = "{tmp_path / "vault"}"\n')
    return ["--config", str(config_path), "--sources", str(tmp_path / "absent.yaml")]


def _json_paths(tmp_path: Path) -> list[str]:
    """A json deployment with every setting in cyris.toml and no sources.yaml."""
    config_path = tmp_path / "cyris.toml"
    config_path.write_text('[store]\nbackend = "json"\n\n' + settings_toml())
    return ["--config", str(config_path), "--sources", str(tmp_path / "absent.yaml")]


def test_a_run_on_a_d1_with_no_sources_ends_without_fetching(
    tmp_path: Path, d1: SqliteD1, built: list[object], caplog
) -> None:
    seed_d1_settings(d1)

    result = runner.invoke(app, ["run", *_d1_paths(tmp_path)])

    assert result.exit_code == 0, result.output
    assert WAITING in caplog.text
    assert "Add one on /settings, or run `cyris sources push`." in caplog.text
    assert built == []


def test_a_json_deployment_with_no_sources_waits_too(
    tmp_path: Path, built: list[object], caplog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CYRIS_STORE_BACKEND", raising=False)

    result = runner.invoke(app, ["run", *_json_paths(tmp_path)])

    assert result.exit_code == 0, result.output
    assert WAITING in caplog.text
    assert "Add feeds to sources.yaml." in caplog.text
    assert built == []


def test_a_scheduled_run_on_a_digest_hour_sends_no_alert(
    tmp_path: Path,
    d1: SqliteD1,
    built: list[object],
    alerts: list[str],
    caplog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_d1_settings(d1)
    _at_hour(monkeypatch, 8)

    result = runner.invoke(app, ["run", "--if-due", *_d1_paths(tmp_path)])

    assert result.exit_code == 0, result.output
    assert WAITING in caplog.text
    assert alerts == []
    assert built == []


def test_a_dry_run_asked_for_a_preview_file_writes_none(
    tmp_path: Path, d1: SqliteD1, built: list[object], caplog
) -> None:
    seed_d1_settings(d1)
    preview = tmp_path / "preview.html"

    result = runner.invoke(
        app, ["run", "--dry-run", "--output", str(preview), *_d1_paths(tmp_path)]
    )

    assert result.exit_code == 1
    assert WAITING in caplog.text
    assert not preview.exists()
    assert built == []


def test_a_run_on_a_d1_with_only_unfetchable_sources_stops(
    tmp_path: Path, d1: SqliteD1, built: list[object], caplog
) -> None:
    """Only an *empty* list waits: a source nothing can fetch is a misconfiguration."""
    from cyris.adapters.store.d1 import apply_schema
    from cyris.adapters.store.source_store import D1SourceStore

    seed_d1_settings(d1)
    apply_schema(d1)
    D1SourceStore(d1).upsert(SourceConfig(name="No Feed"))
    D1SourceStore(d1).upsert(SourceConfig(name="No Sender", type="newsletter"))

    result = runner.invoke(app, ["run", *_d1_paths(tmp_path)])

    assert result.exit_code == 1
    assert (
        "No fetchable sources in D1: No Feed, No Sender. An RSS source needs a feed URL, "
        "a newsletter source a sender match." in caplog.text
    )
    assert WAITING not in caplog.text
    assert built == []
