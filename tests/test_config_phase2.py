"""Tests for Phase 2 config additions."""

import tomllib
from pathlib import Path

from cyris.config import AppConfig


class TestNotifyIsATopLevelTable:
    """`[notify]` is the only table a reader consults for the webhook."""

    def test_the_webhook_is_read_from_the_notify_table(self, monkeypatch):
        raw = tomllib.loads(
            '[notify]\ndiscord_webhook_url = "https://discord.com/api/webhooks/1/tok"\n'
        )

        cfg = AppConfig.model_validate(raw)

        assert cfg.notify.discord_webhook_url == "https://discord.com/api/webhooks/1/tok"

    def test_the_old_nested_stanza_is_dropped_rather_than_honoured(self, monkeypatch):
        """A config left on [general.notify] loses its webhook silently — the run
        still goes out, it just stops notifying until the stanza is moved."""
        raw = tomllib.loads(
            '[general.notify]\ndiscord_webhook_url = "https://discord.com/api/webhooks/1/old"\n'
        )

        cfg = AppConfig.model_validate(raw)

        assert cfg.notify.discord_webhook_url == ""

    def test_general_no_longer_carries_a_notify_field(self, monkeypatch):

        assert hasattr(AppConfig().general, "notify") is False

    def test_the_shipped_example_ships_the_new_table(self):
        example = Path(__file__).parent.parent / "cyris.toml.example"

        raw = tomllib.loads(example.read_text())

        assert "notify" in raw
        assert "[general.notify]" not in example.read_text()
