"""Configuration loader for Cyris.

Loads cyris.toml (app config) and sources.yaml (source definitions).
Sensitive values are injected from environment variables.
"""

import os
import tomllib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from cyris.domain.models import SourceConfig


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
    discord_webhook_url: str = ""

    @model_validator(mode="after")
    def inject_webhook_url(self) -> "NotifyConfig":
        # A webhook URL is a credential: it must have somewhere to live other than
        # the config file, or a deployment that only ships env has no way to set it.
        if not self.discord_webhook_url:
            self.discord_webhook_url = os.environ.get("CYRIS_DISCORD_WEBHOOK_URL", "")
        return self


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


class GeneralConfig(BaseModel):
    digest_schedule: list[str] = Field(default_factory=lambda: ["08:00", "20:00"])
    timezone: str = "Asia/Taipei"
    digest_window_hours: int = Field(default=24, ge=1, le=168)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)


class LLMProviderConfig(BaseModel):
    # No default provider — the user opts in explicitly. Unset ⇒ degraded
    # (excerpt-only) mode instead of silently defaulting to one vendor.
    provider: Literal["anthropic", "gemini"] | None = None
    model: str = ""  # empty ⇒ the provider's default model (see bootstrap.build_llm)
    api_key: str = ""

    @property
    def api_key_env_var(self) -> str:
        return "GEMINI_API_KEY" if self.provider == "gemini" else "ANTHROPIC_API_KEY"

    @model_validator(mode="after")
    def inject_api_key(self) -> "LLMProviderConfig":
        if self.provider and not self.api_key:
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

    @model_validator(mode="after")
    def override_path(self) -> "AgentVaultConfig":
        if p := os.environ.get("CYRIS_AGENT_VAULT_PATH"):
            self.path = Path(p)
        return self


class StoreConfig(BaseModel):
    """Where persistent state lives.

    `json` is local partition files under the agent vault. `d1` puts the article
    store and the usage log in Cloudflare D1, so a dead local machine loses
    nothing. Both read the same data model; run them side by side and diff with
    `cyris store-diff` before switching.
    """

    backend: Literal["json", "d1"] = "json"
    database_id: str = ""
    account_id: str = ""
    api_token: str = ""

    @model_validator(mode="after")
    def inject_credentials(self) -> "StoreConfig":
        if not self.account_id:
            self.account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
        if not self.api_token:
            self.api_token = os.environ.get("CYRIS_D1_API_TOKEN", "") or os.environ.get(
                "CLOUDFLARE_API_TOKEN", ""
            )
        return self

    @property
    def is_d1(self) -> bool:
        return self.backend == "d1"


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


class VoteSimilarityConfig(BaseModel):
    """Suppress candidates that sit close to what the reader downvoted.

    Off by default: it changes what reaches the digest, and the threshold was
    calibrated on one reader's votes. See docs/vote-signal-measurement.md.
    """

    enabled: bool = False
    threshold: float = Field(default=0.68, ge=0.0, le=1.0)
    model: str = "gemini-embedding-001"
    max_seeds: int = Field(default=200, ge=1)


class RssConfig(BaseModel):
    """Cloudflare RSS Worker — the hourly feed buffer the pipeline reads from."""

    worker_url: str = ""
    token: str = ""

    @model_validator(mode="after")
    def inject_token(self) -> "RssConfig":
        if not self.token:
            self.token = os.environ.get("CYRIS_RSS_TOKEN", "")
        return self


class AppConfig(BaseModel):
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    llm_provider: LLMProviderConfig = Field(default_factory=LLMProviderConfig)
    digest: DigestConfig = Field(default_factory=DigestConfig)
    obsidian: ObsidianConfig = Field(default_factory=ObsidianConfig)
    agent_vault: AgentVaultConfig = Field(default_factory=AgentVaultConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    html_output: HtmlOutputConfig = Field(default_factory=HtmlOutputConfig)
    promote: PromoteConfig = Field(default_factory=PromoteConfig)
    newsletter: NewsletterConfig = Field(default_factory=NewsletterConfig)
    rss: RssConfig = Field(default_factory=RssConfig)
    store: StoreConfig = Field(default_factory=StoreConfig)
    vote_similarity: VoteSimilarityConfig = Field(default_factory=VoteSimilarityConfig)


class SourcesConfig(BaseModel):
    defaults: dict[str, str] = Field(default_factory=dict)
    sources: list[SourceConfig] = Field(default_factory=list)
    aliases: dict[str, str] = Field(default_factory=dict)


class Config(BaseModel):
    app: AppConfig
    sources: dict[str, SourceConfig]
    aliases: dict[str, str] = Field(default_factory=dict)

    def validate_required_keys(self) -> None:
        """Raise ValueError if required API keys are missing.

        The LLM is optional: with no provider configured the pipeline runs in
        degraded (excerpt-only) mode, so only a provider that IS set but is
        missing its key counts as an error.
        """
        missing = []
        if self.app.store.is_d1:
            if not self.app.store.database_id:
                missing.append("[store] database_id")
            if not self.app.store.account_id:
                missing.append("CLOUDFLARE_ACCOUNT_ID")
            if not self.app.store.api_token:
                missing.append("CYRIS_D1_API_TOKEN")
        if self.app.llm_provider.provider and not self.app.llm_provider.api_key:
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
