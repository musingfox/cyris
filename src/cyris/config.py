"""Configuration loader for Cyris.

Loads cyris.toml (app config) and sources.yaml (source definitions).
Sensitive values are injected from environment variables.
"""

import os
import tomllib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from cyris.domain.models import SourceConfig
from cyris.domain.tracking import TrackedTopic


def _load_dotenv(env_path: Path | None = None) -> None:
    """Load .env file into os.environ (setdefault, won't override existing)."""
    path = env_path or Path(".env")
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


class NotifyConfig(BaseModel):
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    discord_webhook_url: str = ""


class PaywallConfig(BaseModel):
    use_browser_cookies: bool = False
    browser: str = "chrome"
    cookie_domains: list[str] = Field(default_factory=list)

    @field_validator("browser")
    @classmethod
    def validate_browser(cls, v: str) -> str:
        allowed = {"chrome", "firefox", "edge", "safari", "zen"}
        if v not in allowed:
            raise ValueError(f"browser must be one of {allowed}")
        return v


class EmailConfig(BaseModel):
    webhook_host: str = "0.0.0.0"
    webhook_port: int = Field(default=8765, ge=1, le=65535)
    webhook_path: str = "/webhook/email"
    webhook_secret: str = ""

    @model_validator(mode="after")
    def inject_secret(self) -> "EmailConfig":
        if not self.webhook_secret:
            self.webhook_secret = os.environ.get("CYRIS_EMAIL_WEBHOOK_SECRET", "")
        return self


class RoutingConfig(BaseModel):
    score_threshold: int = Field(default=70, ge=0, le=100)  # Featured article threshold
    summarize_score_threshold: int = Field(default=70, ge=0, le=100)


class ExperimentalConfig(BaseModel):
    dual_pipeline: bool = False


class GeneralConfig(BaseModel):
    digest_schedule: list[str] = Field(default_factory=lambda: ["08:00", "20:00"])
    timezone: str = "Asia/Taipei"
    digest_window_hours: int = Field(default=24, ge=1, le=168)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)


class MinifluxConfig(BaseModel):
    url: str = "http://localhost:8080"
    api_key: str = ""

    @model_validator(mode="after")
    def inject_api_key(self) -> "MinifluxConfig":
        if url := os.environ.get("CYRIS_MINIFLUX_URL"):
            self.url = url
        if not self.api_key:
            self.api_key = os.environ.get("CYRIS_MINIFLUX_API_KEY", "")
        return self


class LLMProviderConfig(BaseModel):
    provider: Literal["anthropic", "gemini"] = "anthropic"
    model: str = "claude-sonnet-4-6"
    api_key: str = ""

    @property
    def api_key_env_var(self) -> str:
        return "GEMINI_API_KEY" if self.provider == "gemini" else "ANTHROPIC_API_KEY"

    @model_validator(mode="after")
    def inject_api_key(self) -> "LLMProviderConfig":
        if not self.api_key:
            self.api_key = os.environ.get(self.api_key_env_var, "")
        return self


class DigestConfig(BaseModel):
    max_articles_per_digest: int = 200
    max_articles_per_digest_output: int = 15
    scoring_snippet_length: int = 1000
    summarize_snippet_length: int = 1000
    filter_snippet_length: int = Field(default=500, ge=1)
    output_language: str = "繁體中文"  # language for headlines/summaries in the digest
    style_prompt: str = ""  # optional reader-defined tone/focus injected into prompts


class ObsidianConfig(BaseModel):
    user_vault_path: Path = Path("~/Documents/ObsidianVault")
    digest_folder: str = "Digests"

    @model_validator(mode="after")
    def expand_path(self) -> "ObsidianConfig":
        if p := os.environ.get("CYRIS_VAULT_PATH"):
            self.user_vault_path = Path(p)
        self.user_vault_path = self.user_vault_path.expanduser()
        return self


class AgentVaultConfig(BaseModel):
    path: Path = Path("./agent-vault")
    tracking_file: Path = Path("tracking.yaml")

    @model_validator(mode="after")
    def override_path(self) -> "AgentVaultConfig":
        if p := os.environ.get("CYRIS_AGENT_VAULT_PATH"):
            self.path = Path(p)
        return self


class VaultConfigSource:
    """Vault-backed impl of TrackingConfigSource (for agent-vault/tracking.yaml)."""

    def __init__(self, tracking_file: Path):
        self.tracking_file = Path(tracking_file)

    async def load_topics(self) -> list[TrackedTopic]:
        from cyris.adapters.tracking_yaml import load_topics_from_file

        return await load_topics_from_file(self.tracking_file)

    async def upsert_topic(self, topic: TrackedTopic) -> None:
        from cyris.adapters.tracking_yaml import upsert_topic_to_file

        await upsert_topic_to_file(self.tracking_file, topic)


class HtmlOutputConfig(BaseModel):
    enabled: bool = False
    output_dir: str = "agent-vault/html"


class PromoteConfig(BaseModel):
    worker_url: str = ""
    publish_enabled: bool = False
    pages_project: str = ""
    token: str = ""

    @model_validator(mode="after")
    def inject_token(self) -> "PromoteConfig":
        if not self.token:
            self.token = os.environ.get("CYRIS_PROMOTE_TOKEN", "")
        return self


class NewsletterConfig(BaseModel):
    worker_url: str = ""
    token: str = ""

    @model_validator(mode="after")
    def inject_token(self) -> "NewsletterConfig":
        if not self.token:
            self.token = os.environ.get("CYRIS_NEWSLETTER_TOKEN", "")
        return self


class AppConfig(BaseModel):
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    miniflux: MinifluxConfig = Field(default_factory=MinifluxConfig)
    llm_provider: LLMProviderConfig = Field(default_factory=LLMProviderConfig)
    digest: DigestConfig = Field(default_factory=DigestConfig)
    obsidian: ObsidianConfig = Field(default_factory=ObsidianConfig)
    agent_vault: AgentVaultConfig = Field(default_factory=AgentVaultConfig)
    paywall: PaywallConfig = Field(default_factory=PaywallConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    experimental: ExperimentalConfig = Field(default_factory=ExperimentalConfig)
    html_output: HtmlOutputConfig = Field(default_factory=HtmlOutputConfig)
    promote: PromoteConfig = Field(default_factory=PromoteConfig)
    newsletter: NewsletterConfig = Field(default_factory=NewsletterConfig)


class SourcesConfig(BaseModel):
    defaults: dict[str, str] = Field(default_factory=dict)
    sources: list[SourceConfig] = Field(default_factory=list)
    aliases: dict[str, str] = Field(default_factory=dict)


class Config(BaseModel):
    app: AppConfig
    sources: dict[str, SourceConfig]
    aliases: dict[str, str] = Field(default_factory=dict)

    def validate_required_keys(self) -> None:
        """Raise ValueError if required API keys are missing."""
        missing = []
        if not self.app.miniflux.api_key:
            missing.append("CYRIS_MINIFLUX_API_KEY")
        if not self.app.llm_provider.api_key:
            missing.append(self.app.llm_provider.api_key_env_var)
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")


def load_config(
    config_path: Path | None = None,
    sources_path: Path | None = None,
) -> Config:
    """Load and validate configuration from TOML and YAML files.

    Args:
        config_path: Path to cyris.toml. Defaults to ./cyris.toml.
        sources_path: Path to sources.yaml. Defaults to ./sources.yaml.

    Returns:
        Validated Config object.

    Raises:
        FileNotFoundError: If config files don't exist.
        ValueError: If required env vars are missing.
        ValidationError: If config structure is invalid.
    """
    config_path = config_path or Path("cyris.toml")
    sources_path = sources_path or Path("sources.yaml")

    # Load .env from same directory as config
    _load_dotenv(config_path.parent / ".env")

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    if not sources_path.exists():
        raise FileNotFoundError(f"Sources file not found: {sources_path}")

    with open(config_path, "rb") as f:
        raw_toml = tomllib.load(f)

    with open(sources_path) as f:
        raw_yaml = yaml.safe_load(f)

    app_config = AppConfig.model_validate(raw_toml)

    sources_config = SourcesConfig.model_validate(raw_yaml or {})

    # Apply defaults to sources
    defaults = sources_config.defaults
    for source in sources_config.sources:
        if source.language == "auto" and "language" in defaults:
            source.language = defaults["language"]

    # Key sources by name for fast lookup
    sources_dict = {s.name: s for s in sources_config.sources}

    return Config(app=app_config, sources=sources_dict, aliases=sources_config.aliases)
