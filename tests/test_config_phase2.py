"""Tests for Phase 2 config additions."""

import pytest

from cyris.config import EmailConfig, NotifyConfig


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


class TestEmailConfig:
    def test_defaults_with_env(self, monkeypatch):
        monkeypatch.setenv("CYRIS_EMAIL_WEBHOOK_SECRET", "secret123")
        cfg = EmailConfig.model_validate({})
        assert cfg.webhook_host == "0.0.0.0"
        assert cfg.webhook_port == 8765
        assert cfg.webhook_path == "/webhook/email"
        assert cfg.webhook_secret == "secret123"

    def test_custom_port(self):
        cfg = EmailConfig.model_validate({"webhook_port": 9000})
        assert cfg.webhook_port == 9000

    def test_invalid_port(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            EmailConfig.model_validate({"webhook_port": 99999})
