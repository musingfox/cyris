"""Tests for configuration loader."""

from pathlib import Path

import pytest

from cyris.adapters.tracking_yaml import TrackingConfigSource
from cyris.config import VaultConfigSource, load_config
from cyris.domain.models import Tier


class TestLoadConfig:
    def test_load_example_configs(self, tmp_path, monkeypatch):
        """Load the example config files successfully."""
        monkeypatch.setenv("CYRIS_MINIFLUX_API_KEY", "test-miniflux-key")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")

        project_root = Path(__file__).parent.parent
        cfg = load_config(
            config_path=project_root / "cyris.example.toml",
            sources_path=project_root / "sources.example.yaml",
        )

        assert cfg.app.general.timezone == "Asia/Taipei"
        assert cfg.app.llm_provider.model == "claude-sonnet-4-6"
        assert cfg.app.miniflux.api_key == "test-miniflux-key"
        assert cfg.app.llm_provider.api_key == "test-anthropic-key"

        # Curated teaching sample: a fixed set covering every source shape.
        assert len(cfg.sources) == 7
        assert cfg.sources["Stratechery"].tier == Tier.SUMMARIZE
        assert cfg.sources["Stratechery"].paywall is True
        assert cfg.sources["TechCrunch"].tier == Tier.FILTER
        # newsletter shape: ingested from email, not RSS
        assert cfg.sources["Benedict Evans Newsletter"].type == "newsletter"
        assert cfg.sources["Benedict Evans Newsletter"].email_match == "from:list@benedictevans.com"
        # language override on a non-English feed
        assert cfg.sources["報導者"].language == "zh"

    def test_path_expansion(self, monkeypatch):
        """User vault path should have ~ expanded."""
        monkeypatch.setenv("CYRIS_MINIFLUX_API_KEY", "key")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key")

        project_root = Path(__file__).parent.parent
        cfg = load_config(
            config_path=project_root / "cyris.example.toml",
            sources_path=project_root / "sources.example.yaml",
        )

        assert "~" not in str(cfg.app.obsidian.user_vault_path)
        assert cfg.app.obsidian.user_vault_path.is_absolute()

    def test_missing_config_file(self):
        with pytest.raises(FileNotFoundError, match="Config file not found"):
            load_config(config_path=Path("nonexistent.toml"))

    def test_missing_sources_file(self, tmp_path):
        config_file = tmp_path / "cyris.toml"
        config_file.write_text('[general]\ntimezone = "UTC"\n')
        with pytest.raises(FileNotFoundError, match="Sources file not found"):
            load_config(config_path=config_file, sources_path=Path("nonexistent.yaml"))

    def test_missing_required_env_vars(self, tmp_path, monkeypatch):
        """validate_required_keys should raise if env vars are missing."""
        monkeypatch.delenv("CYRIS_MINIFLUX_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        # Use tmp_path so _load_dotenv won't find the real .env
        project_root = Path(__file__).parent.parent
        config_copy = tmp_path / "cyris.toml"
        sources_copy = tmp_path / "sources.yaml"
        config_copy.write_bytes((project_root / "cyris.example.toml").read_bytes())
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


class TestExperimentalConfig:
    def test_experimental_config_default(self):
        """ExperimentalConfig default dual_pipeline is False."""
        from cyris.config import ExperimentalConfig

        config = ExperimentalConfig()
        assert config.dual_pipeline is False

    def test_experimental_config_explicit(self):
        """ExperimentalConfig accepts explicit dual_pipeline value."""
        from cyris.config import ExperimentalConfig

        config = ExperimentalConfig(dual_pipeline=True)
        assert config.dual_pipeline is True


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
    def test_provider_defaults_to_anthropic(self, monkeypatch):
        """LLMProviderConfig defaults to the anthropic provider and ANTHROPIC_API_KEY."""
        from cyris.config import LLMProviderConfig

        monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key")
        monkeypatch.setenv("GEMINI_API_KEY", "g-key")
        config = LLMProviderConfig()
        assert config.provider == "anthropic"
        assert config.api_key == "a-key"

    def test_gemini_provider_reads_gemini_key(self, monkeypatch):
        """provider=gemini injects GEMINI_API_KEY instead of ANTHROPIC_API_KEY."""
        from cyris.config import LLMProviderConfig

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "g-key")
        config = LLMProviderConfig(provider="gemini")
        assert config.api_key == "g-key"

    def test_invalid_provider_rejected(self):
        """Unknown provider values fail validation."""
        from pydantic import ValidationError

        from cyris.config import LLMProviderConfig

        with pytest.raises(ValidationError):
            LLMProviderConfig(provider="openai")

    def test_missing_gemini_key_named_in_error(self, monkeypatch):
        """validate_required_keys names GEMINI_API_KEY when provider=gemini."""
        from cyris.config import AppConfig, Config, LLMProviderConfig, MinifluxConfig

        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        cfg = Config(
            app=AppConfig(
                miniflux=MinifluxConfig(api_key="test-key"),
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

        claude = build_llm(LLMProviderConfig(api_key="k"))
        assert isinstance(claude, AnthropicClient)


class TestVaultTrackingProtocol:
    def test_vault_config_source_implements_tracking_config_source(self, tmp_path):
        """T5: isinstance(VaultConfigSource, TrackingConfigSource) True."""
        v = VaultConfigSource(tmp_path / "t.yaml")
        assert isinstance(v, TrackingConfigSource)
