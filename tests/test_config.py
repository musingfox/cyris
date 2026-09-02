"""Tests for configuration loader."""

import logging
from pathlib import Path

import pytest
import tomllib

from cyris.config import load_config
from cyris.domain.models import Tier


class TestLoadConfig:
    def test_load_example_configs(self, tmp_path, monkeypatch):
        """Load the example config files successfully."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")

        project_root = Path(__file__).parent.parent
        cfg = load_config(
            config_path=project_root / "cyris.toml.example",
            sources_path=project_root / "sources.example.yaml",
        )

        assert cfg.app.general.timezone == "Asia/Taipei"
        assert cfg.app.llm_provider.model == "claude-sonnet-4-6"
        assert cfg.app.llm_provider.api_key == "test-anthropic-key"

        # Curated teaching sample covering the main source shapes.
        assert len(cfg.sources) == 5
        assert cfg.sources["Stratechery"].tier == Tier.SUMMARIZE
        assert cfg.sources["TechCrunch"].tier == Tier.FILTER
        # email-only newsletter shape: no url, matched by From: header
        assert cfg.sources["Some Email Newsletter"].type == "newsletter"
        assert cfg.sources["Some Email Newsletter"].url is None
        assert cfg.sources["Some Email Newsletter"].email_match == "from:newsletter@example.com"
        # language override on a non-English feed
        assert cfg.sources["報導者"].language == "zh"

    def test_missing_config_file(self, tmp_path):
        sources = tmp_path / "sources.yaml"
        sources.write_bytes((Path(__file__).parent.parent / "sources.example.yaml").read_bytes())
        missing = tmp_path / "nope.toml"
        cfg = load_config(config_path=missing, sources_path=sources)
        assert cfg.app.general.timezone == "Asia/Taipei"
        assert cfg.app.store.backend == "json"
        assert cfg.config_file_found is False

    def test_present_config_file_is_used(self, tmp_path):
        config_file = tmp_path / "cyris.toml"
        config_file.write_text('[general]\ntimezone = "UTC"\n')
        sources = tmp_path / "sources.yaml"
        sources.write_bytes((Path(__file__).parent.parent / "sources.example.yaml").read_bytes())
        cfg = load_config(config_path=config_file, sources_path=sources)
        assert cfg.app.general.timezone == "UTC"
        assert cfg.config_file_found is True

    def test_malformed_config_file_raises(self, tmp_path):
        config_file = tmp_path / "cyris.toml"
        config_file.write_text("[general\n")
        sources = tmp_path / "sources.yaml"
        sources.write_bytes((Path(__file__).parent.parent / "sources.example.yaml").read_bytes())
        with pytest.raises(tomllib.TOMLDecodeError):
            load_config(config_path=config_file, sources_path=sources)

    def test_missing_config_file_warns(self, tmp_path, caplog):
        sources = tmp_path / "sources.yaml"
        sources.write_bytes((Path(__file__).parent.parent / "sources.example.yaml").read_bytes())
        missing = tmp_path / "nope.toml"
        with caplog.at_level(logging.WARNING):
            load_config(config_path=missing, sources_path=sources)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert str(missing) in warnings[0].message

    def test_missing_sources_file(self, tmp_path):
        config_file = tmp_path / "cyris.toml"
        config_file.write_text('[general]\ntimezone = "UTC"\n')
        with pytest.raises(FileNotFoundError, match="Sources file not found"):
            load_config(config_path=config_file, sources_path=Path("nonexistent.yaml"))

    def test_missing_required_env_vars(self, tmp_path, monkeypatch):
        """validate_required_keys should raise if env vars are missing."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        # Use tmp_path so _load_dotenv won't find the real .env
        project_root = Path(__file__).parent.parent
        config_copy = tmp_path / "cyris.toml"
        sources_copy = tmp_path / "sources.yaml"
        config_copy.write_bytes((project_root / "cyris.toml.example").read_bytes())
        sources_copy.write_bytes((project_root / "sources.example.yaml").read_bytes())

        cfg = load_config(config_path=config_copy, sources_path=sources_copy)

        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
            cfg.validate_required_keys()


class TestRoutingConfig:
    def test_routing_config_default(self):
        """RoutingConfig default threshold is 70 (featured article threshold)."""
        from cyris.config import RoutingConfig

        config = RoutingConfig()
        assert config.score_threshold == 70
        assert config.summarize_score_threshold == 70

    def test_routing_config_valid(self):
        """RoutingConfig accepts valid threshold."""
        from cyris.config import RoutingConfig

        config = RoutingConfig(score_threshold=70)
        assert config.score_threshold == 70

    def test_routing_config_invalid(self):
        """RoutingConfig rejects threshold > 100."""
        from pydantic import ValidationError

        from cyris.config import RoutingConfig

        with pytest.raises(ValidationError):
            RoutingConfig(score_threshold=150)

        with pytest.raises(ValidationError):
            RoutingConfig(score_threshold=-10)

    def test_summarize_score_threshold_default(self):
        """RoutingConfig default summarize_score_threshold is 70."""
        from cyris.config import RoutingConfig

        config = RoutingConfig()
        assert config.summarize_score_threshold == 70

    def test_summarize_score_threshold_custom(self):
        """RoutingConfig accepts custom summarize_score_threshold."""
        from cyris.config import RoutingConfig

        config = RoutingConfig(summarize_score_threshold=80)
        assert config.summarize_score_threshold == 80

    def test_summarize_score_threshold_validation(self):
        """RoutingConfig rejects invalid summarize_score_threshold."""
        from pydantic import ValidationError

        from cyris.config import RoutingConfig

        with pytest.raises(ValidationError):
            RoutingConfig(summarize_score_threshold=150)

        with pytest.raises(ValidationError):
            RoutingConfig(summarize_score_threshold=-5)


class TestDigestConfigSnippetLength:
    def test_digest_config_default_snippet_lengths(self):
        """DigestConfig snippet_length fields default to 1000."""
        from cyris.config import DigestConfig

        config = DigestConfig()
        assert config.scoring_snippet_length == 1000
        assert config.summarize_snippet_length == 1000

    def test_digest_config_custom_scoring_snippet_length(self):
        """DigestConfig accepts custom scoring_snippet_length."""
        from cyris.config import DigestConfig

        config = DigestConfig(scoring_snippet_length=500)
        assert config.scoring_snippet_length == 500
        assert config.summarize_snippet_length == 1000

    def test_digest_config_custom_summarize_snippet_length(self):
        """DigestConfig accepts custom summarize_snippet_length."""
        from cyris.config import DigestConfig

        config = DigestConfig(summarize_snippet_length=1500)
        assert config.scoring_snippet_length == 1000
        assert config.summarize_snippet_length == 1500

    def test_digest_config_both_custom_snippet_lengths(self):
        """DigestConfig accepts both custom snippet lengths."""
        from cyris.config import DigestConfig

        config = DigestConfig(scoring_snippet_length=800, summarize_snippet_length=1200)
        assert config.scoring_snippet_length == 800
        assert config.summarize_snippet_length == 1200

    def test_digest_config_filter_snippet_length_default(self):
        """LLMProviderConfig filter_snippet_length defaults to 500."""
        from cyris.config import DigestConfig

        config = DigestConfig()
        assert config.filter_snippet_length == 500

    def test_digest_config_filter_snippet_length_custom(self):
        """DigestConfig accepts custom filter_snippet_length."""
        from cyris.config import DigestConfig

        config = DigestConfig(filter_snippet_length=800)
        assert config.filter_snippet_length == 800

    def test_digest_config_filter_snippet_length_validation(self):
        """DigestConfig rejects negative filter_snippet_length."""
        from pydantic import ValidationError

        from cyris.config import DigestConfig

        with pytest.raises(ValidationError):
            DigestConfig(filter_snippet_length=-1)

        with pytest.raises(ValidationError):
            DigestConfig(filter_snippet_length=0)


class TestLLMProvider:
    def test_provider_defaults_to_none(self, monkeypatch):
        """No provider by default ⇒ degraded mode, not a silent vendor default."""
        from cyris.config import LLMProviderConfig

        monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key")
        monkeypatch.setenv("GEMINI_API_KEY", "g-key")
        config = LLMProviderConfig()
        assert config.provider is None
        assert config.api_key == ""  # no key injected without a chosen provider

    def test_gemini_provider_reads_gemini_key(self, monkeypatch):
        """provider=gemini injects GEMINI_API_KEY instead of ANTHROPIC_API_KEY."""
        from cyris.config import LLMProviderConfig

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "g-key")
        config = LLMProviderConfig(provider="gemini")
        assert config.api_key == "g-key"

    def test_workers_ai_reads_its_own_token_and_account(self, monkeypatch):
        from cyris.config import LLMProviderConfig

        monkeypatch.setenv("CLOUDFLARE_AI_TOKEN", "ai-token")
        monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct-1")
        config = LLMProviderConfig(provider="workers_ai")
        assert config.api_key == "ai-token"
        assert config.account_id == "acct-1"

    def test_workers_ai_falls_back_to_the_embedding_token(self, monkeypatch):
        """Same 'Workers AI -> Read' permission, so an embed-compare setup needs nothing new."""
        from cyris.config import LLMProviderConfig

        monkeypatch.delenv("CLOUDFLARE_AI_TOKEN", raising=False)
        monkeypatch.setenv("CLOUDFLARE_EMBEDDING_API_TOKEN", "embed-token")
        assert LLMProviderConfig(provider="workers_ai").api_key == "embed-token"

    def test_workers_ai_never_uses_the_d1_pages_token(self, monkeypatch):
        """CLOUDFLARE_API_TOKEN carries D1 and Pages; using it here would 403 confusingly."""
        from cyris.config import LLMProviderConfig

        monkeypatch.delenv("CLOUDFLARE_AI_TOKEN", raising=False)
        monkeypatch.delenv("CLOUDFLARE_EMBEDDING_API_TOKEN", raising=False)
        monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "d1-pages-token")
        assert LLMProviderConfig(provider="workers_ai").api_key == ""

    def test_invalid_provider_rejected(self):
        """Unknown provider values fail validation."""
        from pydantic import ValidationError

        from cyris.config import LLMProviderConfig

        with pytest.raises(ValidationError):
            LLMProviderConfig(provider="mistral")

    def test_missing_gemini_key_named_in_error(self, monkeypatch):
        """validate_required_keys names GEMINI_API_KEY when provider=gemini."""
        from cyris.config import AppConfig, Config, LLMProviderConfig

        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        cfg = Config(
            app=AppConfig(
                llm_provider=LLMProviderConfig(provider="gemini"),
            ),
            sources={},
        )
        with pytest.raises(ValueError, match="GEMINI_API_KEY"):
            cfg.validate_required_keys()

    def test_build_llm_selects_gemini(self):
        """build_llm returns GeminiClient for provider=gemini, AnthropicClient otherwise."""
        from cyris.adapters.anthropic_client import AnthropicClient
        from cyris.adapters.gemini_client import GeminiClient
        from cyris.bootstrap import build_llm
        from cyris.config import LLMProviderConfig

        gemini = build_llm(
            LLMProviderConfig(provider="gemini", model="gemini-2.5-flash", api_key="k")
        )
        assert isinstance(gemini, GeminiClient)
        assert gemini.model == "gemini-2.5-flash"

        claude = build_llm(LLMProviderConfig(provider="anthropic", api_key="k"))
        assert isinstance(claude, AnthropicClient)

        # No provider ⇒ None (degraded mode)
        assert build_llm(LLMProviderConfig()) is None
