"""A scheduled run that cannot start alerts the configured channels, on a digest hour only.

The cron tick is hourly, and a broken configuration fails every tick, so an
alert on each would be 24 a day; the two digest hours are the ones a reader
would otherwise notice by an issue that never came.
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fakes import SqliteD1, seed_d1_settings
from typer.testing import CliRunner

from cyris.domain.models import SourceConfig
from cyris.entrypoints.cli import app

pytestmark = pytest.mark.integration

runner = CliRunner()

WEBHOOK = "https://discord.com/api/webhooks/1/tok"


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[tuple]]:
    sent: dict[str, list[tuple]] = {"discord": [], "mail": []}

    async def discord(webhook_url: str, subject: str, text: str) -> None:
        sent["discord"].append((webhook_url, subject, text))

    async def mail(to: str, sender: str, subject: str, text: str) -> None:
        sent["mail"].append((to, sender, subject, text))

    monkeypatch.setattr("cyris.adapters.notify.send_discord_alert", discord)
    monkeypatch.setattr("cyris.bootstrap.build_send_email_alert", lambda: mail)
    return sent


def _at_hour(monkeypatch: pytest.MonkeyPatch, hour: int) -> None:
    monkeypatch.setattr(
        "cyris.utils.timezone.now_in_timezone",
        lambda tz: datetime(2026, 9, 19, hour, 5, tzinfo=ZoneInfo(tz)),
    )


def _paths(tmp_path: Path) -> list[str]:
    config_path = tmp_path / "cyris.toml"
    config_path.write_text(f'[agent_vault]\npath = "{tmp_path / "vault"}"\n')
    return ["--config", str(config_path), "--sources", str(tmp_path / "sources.yaml")]


def _one_source(d1: SqliteD1) -> None:
    from cyris.adapters.store.d1 import apply_schema
    from cyris.adapters.store.source_store import D1SourceStore

    apply_schema(d1)
    D1SourceStore(d1).upsert(SourceConfig(name="Feed", url="https://a.test/feed"))


def _failing_build_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    def build_deps(*_args, **_kwargs):
        raise RuntimeError("D1 token rejected")

    monkeypatch.setattr("cyris.bootstrap.build_deps", build_deps)


def test_incomplete_settings_on_a_digest_hour_alert_every_channel(
    tmp_path: Path, d1: SqliteD1, alerts, monkeypatch
) -> None:
    seed_d1_settings(
        d1,
        omit=["digest.max_featured"],
        **{
            "notify.discord_webhook_url": WEBHOOK,
            "notify.email_to": "reader@a.test",
            "notify.email_from": "cyris@a.test",
        },
    )
    _one_source(d1)
    _at_hour(monkeypatch, 8)

    result = runner.invoke(app, ["run", "--if-due", *_paths(tmp_path)])

    assert result.exit_code == 1
    [(webhook, subject, text)] = alerts["discord"]
    assert webhook == WEBHOOK
    assert subject == "Digest run could not start: morning, 2026-09-19 08:05 Asia/Taipei"
    assert "digest.max_featured" in text
    [(to, sender, mail_subject, mail_text)] = alerts["mail"]
    assert (to, sender, mail_subject, mail_text) == ("reader@a.test", "cyris@a.test", subject, text)


def test_incomplete_settings_off_the_hour_alert_nobody(
    tmp_path: Path, d1: SqliteD1, alerts, monkeypatch
) -> None:
    seed_d1_settings(d1, omit=["digest.max_featured"], **{"notify.discord_webhook_url": WEBHOOK})
    _one_source(d1)
    _at_hour(monkeypatch, 11)

    result = runner.invoke(app, ["run", "--if-due", *_paths(tmp_path)])

    assert result.exit_code == 1
    assert alerts == {"discord": [], "mail": []}


def test_settings_without_a_schedule_alert_nobody(
    tmp_path: Path, d1: SqliteD1, alerts, monkeypatch
) -> None:
    # Whether this hour is a digest hour cannot be known, and guessing means 24 alerts a day.
    seed_d1_settings(
        d1, omit=["general.digest_schedule"], **{"notify.discord_webhook_url": WEBHOOK}
    )
    _one_source(d1)
    _at_hour(monkeypatch, 8)

    result = runner.invoke(app, ["run", "--if-due", *_paths(tmp_path)])

    assert result.exit_code == 1
    assert alerts == {"discord": [], "mail": []}


def test_a_failed_build_on_a_digest_hour_alerts(
    tmp_path: Path, d1: SqliteD1, alerts, monkeypatch
) -> None:
    seed_d1_settings(d1, **{"notify.discord_webhook_url": WEBHOOK})
    _one_source(d1)
    _at_hour(monkeypatch, 20)
    _failing_build_deps(monkeypatch)

    result = runner.invoke(app, ["run", "--if-due", *_paths(tmp_path)])

    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)
    [(_webhook, subject, text)] = alerts["discord"]
    assert subject == "Digest run could not start: evening, 2026-09-19 20:05 Asia/Taipei"
    assert text == "RuntimeError: D1 token rejected"
    # No address is set, so no mail goes out.
    assert alerts["mail"] == []


def test_a_run_started_by_hand_alerts_nobody(
    tmp_path: Path, d1: SqliteD1, alerts, monkeypatch
) -> None:
    # Whoever typed it is reading the error already.
    seed_d1_settings(d1, **{"notify.discord_webhook_url": WEBHOOK})
    _one_source(d1)
    _at_hour(monkeypatch, 8)
    _failing_build_deps(monkeypatch)

    result = runner.invoke(app, ["run", *_paths(tmp_path)])

    assert isinstance(result.exception, RuntimeError)
    assert alerts == {"discord": [], "mail": []}


def test_a_failed_alert_leaves_the_exit_code(
    tmp_path: Path, d1: SqliteD1, alerts, monkeypatch
) -> None:
    async def broken(*_args) -> None:
        raise OSError("Discord unreachable")

    monkeypatch.setattr("cyris.adapters.notify.send_discord_alert", broken)
    seed_d1_settings(d1, omit=["digest.max_featured"], **{"notify.discord_webhook_url": WEBHOOK})
    _one_source(d1)
    _at_hour(monkeypatch, 8)

    result = runner.invoke(app, ["run", "--if-due", *_paths(tmp_path)])

    assert result.exit_code == 1
