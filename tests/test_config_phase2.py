"""Tests for Phase 2 config additions."""

import pytest

from cyris.config import EmailConfig


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
