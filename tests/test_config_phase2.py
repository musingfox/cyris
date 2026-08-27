"""Tests for Phase 2 config additions."""

from cyris.config import NotifyConfig


class TestNotifyConfig:
    def test_webhook_url_falls_back_to_env(self, monkeypatch):
        monkeypatch.setenv("CYRIS_DISCORD_WEBHOOK_URL", "https://discord.test/hook")
        assert NotifyConfig.model_validate({}).discord_webhook_url == "https://discord.test/hook"

    def test_config_file_value_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("CYRIS_DISCORD_WEBHOOK_URL", "https://discord.test/env")
        cfg = NotifyConfig.model_validate({"discord_webhook_url": "https://discord.test/toml"})
        assert cfg.discord_webhook_url == "https://discord.test/toml"

    def test_empty_when_neither_is_set(self, monkeypatch):
        monkeypatch.delenv("CYRIS_DISCORD_WEBHOOK_URL", raising=False)
        assert NotifyConfig.model_validate({}).discord_webhook_url == ""
