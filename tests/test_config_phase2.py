"""Tests for Phase 2 config additions."""

import pytest

from cyris.config import EmailConfig, PaywallConfig


class TestPaywallConfig:
    def test_valid_full(self):
        cfg = PaywallConfig.model_validate(
            {
                "use_browser_cookies": True,
                "browser": "chrome",
                "cookie_domains": ["stratechery.com"],
            }
        )
        assert cfg.use_browser_cookies is True
        assert cfg.browser == "chrome"
        assert cfg.cookie_domains == ["stratechery.com"]

    def test_defaults(self):
        cfg = PaywallConfig.model_validate({})
        assert cfg.use_browser_cookies is False
        assert cfg.browser == "chrome"
        assert cfg.cookie_domains == []

    def test_invalid_browser(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            PaywallConfig.model_validate({"browser": "invalid"})

    def test_paywall_config_accepts_zen(self):
        """Test that 'zen' is a valid browser choice."""
        cfg = PaywallConfig.model_validate(
            {
                "use_browser_cookies": True,
                "browser": "zen",
                "cookie_domains": ["x.com"],
            }
        )
        assert cfg.browser == "zen"
        assert cfg.use_browser_cookies is True
        assert cfg.cookie_domains == ["x.com"]


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
