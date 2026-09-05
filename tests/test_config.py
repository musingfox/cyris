"""Tests for configuration loader."""

import inspect
import logging
import tomllib
from pathlib import Path

import pytest

from cyris.config import load_config
from cyris.domain.models import Tier


def _copy_example_sources(tmp_path: Path) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_bytes((Path(__file__).parent.parent / "sources.example.yaml").read_bytes())
    return path


def _load_tmp(tmp_path: Path, config_path: Path):
    return load_config(config_path=config_path, sources_path=_copy_example_sources(tmp_path))


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
        cfg = load_config(config_path=config_file, sources_path=tmp_path / "nope.yaml")
        assert cfg.sources == {}

    def test_missing_sources_fails_doctor(self, tmp_path):
        from cyris.diagnostics.doctor import _check_sources

        config_file = tmp_path / "cyris.toml"
        config_file.write_text('[general]\ntimezone = "UTC"\n')
        cfg = load_config(config_path=config_file, sources_path=tmp_path / "nope.yaml")
        check = _check_sources(cfg)
        assert check.name == "sources"
        assert check.status == "fail"
        assert check.detail == "no usable sources"

    def test_sources_file_is_used(self, tmp_path):
        config_file = tmp_path / "cyris.toml"
        config_file.write_text('[general]\ntimezone = "UTC"\n')
        sources = tmp_path / "sources.yaml"
        sources.write_text("sources:\n  - name: X\n    url: https://x/feed\n    tier: filter\n")
        cfg = load_config(config_path=config_file, sources_path=sources)
        assert set(cfg.sources) == {"X"}

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


class TestPublishingSettingsFromEnv:
    def test_no_file_reads_enabled_and_pages_project(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CYRIS_HTML_OUTPUT_ENABLED", "true")
        monkeypatch.setenv("CYRIS_PROMOTE_PUBLISH_ENABLED", "1")
        monkeypatch.setenv("CYRIS_PROMOTE_PAGES_PROJECT", "cyris-digest")
        cfg = _load_tmp(tmp_path, tmp_path / "nope.toml")
        assert cfg.app.html_output.enabled is True
        assert cfg.app.promote.publish_enabled is True
        assert cfg.app.promote.pages_project == "cyris-digest"

    def test_enabled_false_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CYRIS_HTML_OUTPUT_ENABLED", "false")
        cfg = _load_tmp(tmp_path, tmp_path / "nope.toml")
        assert cfg.app.html_output.enabled is False

    def test_invalid_bool_raises(self, monkeypatch):
        from pydantic import ValidationError

        from cyris.config import AppConfig

        monkeypatch.setenv("CYRIS_HTML_OUTPUT_ENABLED", "maybe")
        with pytest.raises(ValidationError):
            AppConfig.model_validate({})

    def test_file_bool_wins_over_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CYRIS_HTML_OUTPUT_ENABLED", "true")
        config_file = tmp_path / "cyris.toml"
        config_file.write_text("[html_output]\nenabled = false\n")
        cfg = _load_tmp(tmp_path, config_file)
        assert cfg.app.html_output.enabled is False

    def test_empty_file_pages_project_yields_to_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CYRIS_PROMOTE_PAGES_PROJECT", "cyris-digest")
        config_file = tmp_path / "cyris.toml"
        config_file.write_text('[promote]\npages_project = ""\n')
        cfg = _load_tmp(tmp_path, config_file)
        assert cfg.app.promote.pages_project == "cyris-digest"

    def test_custom_domain_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CYRIS_PROMOTE_CUSTOM_DOMAIN", "digest.example.org")
        cfg = _load_tmp(tmp_path, tmp_path / "nope.toml")
        assert cfg.app.promote.custom_domain == "digest.example.org"


class TestStoreBackendFromEnv:
    def test_env_selects_d1_store(self, monkeypatch):
        from cyris.bootstrap import build_store
        from cyris.config import AppConfig, Config

        monkeypatch.setenv("CYRIS_STORE_BACKEND", "d1")
        monkeypatch.setenv("CYRIS_STORE_DATABASE_ID", "abc")
        monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
        monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
        cfg = Config(app=AppConfig.model_validate({}), sources={})
        assert cfg.app.store.is_d1 is True
        assert cfg.app.store.database_id == "abc"
        assert type(build_store(cfg)).__name__ == "D1ArticleStore"

    def test_no_file_defaults_to_json(self, tmp_path):
        from cyris.bootstrap import build_store

        cfg = _load_tmp(tmp_path, tmp_path / "nope.toml")
        assert cfg.app.store.backend == "json"
        assert type(build_store(cfg)).__name__ == "ArticleStore"

    def test_invalid_backend_raises(self, monkeypatch):
        from pydantic import ValidationError

        from cyris.config import AppConfig

        monkeypatch.setenv("CYRIS_STORE_BACKEND", "sqlite")
        with pytest.raises(ValidationError, match="backend"):
            AppConfig.model_validate({})

    def test_file_backend_wins_over_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CYRIS_STORE_BACKEND", "d1")
        config_file = tmp_path / "cyris.toml"
        config_file.write_text('[store]\nbackend = "json"\n')
        cfg = _load_tmp(tmp_path, config_file)
        assert cfg.app.store.backend == "json"

    def test_empty_database_id_yields_to_env(self, monkeypatch):
        import tomllib

        from cyris.config import AppConfig

        monkeypatch.setenv("CYRIS_STORE_DATABASE_ID", "abc")
        raw = tomllib.loads('[store]\nbackend = "d1"\ndatabase_id = ""\n')
        cfg_app = AppConfig.model_validate(raw)
        assert cfg_app.store.database_id == "abc"

    def test_d1_without_database_id_names_env_var(self, monkeypatch):
        from cyris.config import AppConfig, Config

        monkeypatch.setenv("CYRIS_STORE_BACKEND", "d1")
        cfg = Config(app=AppConfig.model_validate({}), sources={})
        with pytest.raises(ValueError, match="CYRIS_STORE_DATABASE_ID"):
            cfg.validate_required_keys()

    def test_d1_client_refuses_empty_credentials_before_any_request(self, monkeypatch):
        """`backend = d1` with blank creds must name the variable, not retry HTTP 4x."""
        from cyris import bootstrap
        from cyris.adapters.store import d1 as d1_module
        from cyris.config import AppConfig, Config

        monkeypatch.setenv("CYRIS_STORE_BACKEND", "d1")
        monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
        monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)

        constructed: list[object] = []

        class ExplodingD1Client:
            def __init__(self, **kwargs):
                constructed.append(kwargs)
                raise AssertionError("D1Client must not be constructed without credentials")

        monkeypatch.setattr(d1_module, "D1Client", ExplodingD1Client)

        cfg = Config(app=AppConfig.model_validate({}), sources={})
        with pytest.raises(ValueError, match="CYRIS_STORE_DATABASE_ID"):
            bootstrap.build_d1_client(cfg)
        assert constructed == []

    def test_sources_read_skips_d1_when_credentials_are_missing(self, tmp_path, monkeypatch):
        """The fallback to sources.yaml must not cost four doomed requests first."""
        from cyris.adapters.store import d1 as d1_module

        monkeypatch.setenv("CYRIS_STORE_BACKEND", "d1")
        monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
        monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)

        attempts: list[dict] = []

        class RecordingD1Client:
            def __init__(self, **kwargs):
                # `_sources_from_d1` swallows everything, so record rather than raise.
                attempts.append(kwargs)
                raise RuntimeError("no credentials")

        monkeypatch.setattr(d1_module, "D1Client", RecordingD1Client)

        cfg = _load_tmp(tmp_path, tmp_path / "nope.toml")
        assert attempts == []
        assert cfg.sources_origin == "sources.yaml"
        assert cfg.sources

    def test_b_grade_registry_names_store_keys(self):
        from cyris.config import B_GRADE_ENV_VARS

        assert B_GRADE_ENV_VARS["store.backend"] == "CYRIS_STORE_BACKEND"
        assert B_GRADE_ENV_VARS["store.database_id"] == "CYRIS_STORE_DATABASE_ID"


class TestEmptyEnvIsUnset:
    def test_blank_b_grade_vars_leave_defaults(self, tmp_path, monkeypatch):
        """`cp .env.example .env` exports every CYRIS_* key as "" — that is not a value.

        The autouse fixture clears these, so set them back explicitly.
        """
        from cyris.config import B_GRADE_ENV_VARS

        for name in B_GRADE_ENV_VARS.values():
            monkeypatch.setenv(name, "")
        cfg = _load_tmp(tmp_path, tmp_path / "nope.toml")
        assert cfg.app.store.backend == "json"
        assert cfg.app.html_output.enabled is False
        assert cfg.app.promote.publish_enabled is False
        assert cfg.app.rss.worker_url == ""


class TestWorkerUrlsFromEnv:
    def test_rss_url_and_token_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CYRIS_RSS_WORKER_URL", "https://rss.example.dev")
        monkeypatch.setenv("CYRIS_WORKER_TOKEN", "t")
        cfg = _load_tmp(tmp_path, tmp_path / "nope.toml")
        assert cfg.app.rss.worker_url == "https://rss.example.dev"
        assert cfg.app.rss.token == "t"

    def test_promote_url_does_not_bleed_into_newsletter(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CYRIS_PROMOTE_WORKER_URL", "https://p.example.dev")
        cfg = _load_tmp(tmp_path, tmp_path / "nope.toml")
        assert cfg.app.promote.worker_url == "https://p.example.dev"
        assert cfg.app.newsletter.worker_url == ""

    def test_file_newsletter_url_wins_over_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CYRIS_NEWSLETTER_WORKER_URL", "https://env.example")
        config_file = tmp_path / "cyris.toml"
        config_file.write_text('[newsletter]\nworker_url = "https://file.example"\n')
        cfg = _load_tmp(tmp_path, config_file)
        assert cfg.app.newsletter.worker_url == "https://file.example"

    def test_empty_file_rss_url_yields_to_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CYRIS_RSS_WORKER_URL", "https://env.example")
        config_file = tmp_path / "cyris.toml"
        config_file.write_text('[rss]\nworker_url = ""\n')
        cfg = _load_tmp(tmp_path, config_file)
        assert cfg.app.rss.worker_url == "https://env.example"


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


def test_code_defaults_match_the_config_defaults_they_shadow() -> None:
    """Two settings state their default twice; neither may drift from the other.

    Both function defaults are only reached by callers that omit the value — the
    pipeline always passes the configured one — so a drift would show up nowhere
    except in whatever calls them directly.
    """
    from cyris.config import AppConfig
    from cyris.domain.selection import layer_by_score
    from cyris.service_layer.prompts import DEFAULT_LANGUAGE

    defaults = AppConfig()
    assert defaults.digest.output_language == DEFAULT_LANGUAGE
    assert (
        inspect.signature(layer_by_score).parameters["featured_threshold"].default
        == defaults.routing.score_threshold
    )
