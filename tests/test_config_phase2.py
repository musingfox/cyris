"""Tests for Phase 2 config additions."""

import tomllib
from pathlib import Path

from fakes import settings_toml

from cyris.config import GeneralConfig, load_config


def _load(tmp_path: Path, toml: str):
    (tmp_path / "cyris.toml").write_text(toml)
    return load_config(tmp_path / "cyris.toml", tmp_path / "sources.yaml")


class TestNotifyIsATopLevelTable:
    """`[notify]` is the only table a reader consults for the webhook."""

    def test_the_webhook_is_read_from_the_notify_table(self, tmp_path):
        url = "https://discord.com/api/webhooks/1/tok"

        cfg = _load(tmp_path, settings_toml(**{"notify.discord_webhook_url": url}))

        assert cfg.app.notify.discord_webhook_url == url

    def test_the_old_nested_stanza_is_not_read_as_the_webhook(self, tmp_path):
        """A config left on [general.notify] does not set the webhook: it is missing."""
        cfg = _load(
            tmp_path,
            settings_toml(omit=["notify.discord_webhook_url"])
            + '[general.notify]\ndiscord_webhook_url = "https://discord.com/api/webhooks/1/old"\n',
        )

        assert cfg.app.notify is None
        assert "notify.discord_webhook_url" in cfg.missing_settings

    def test_general_no_longer_carries_a_notify_field(self):
        assert "notify" not in GeneralConfig.model_fields

    def test_the_shipped_example_ships_the_new_table(self):
        example = Path(__file__).parent.parent / "cyris.toml.example"

        raw = tomllib.loads(example.read_text())

        assert "notify" in raw
        assert "[general.notify]" not in example.read_text()
