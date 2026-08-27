"""Configuration loader for Cyris.

Loads cyris.toml (app config) and sources.yaml (source definitions).
Sensitive values are injected from environment variables.
"""

import logging
import os
import re
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


logger = logging.getLogger(__name__)


class NotifyConfig(BaseModel):
    discord_webhook_url: str = ""

    @model_validator(mode="after")
    def inject_webhook_url(self) -> "NotifyConfig":
        # A webhook URL is a credential: it must have somewhere to live other than
        # the config file, or a deployment that only ships env has no way to set it.
        if not self.discord_webhook_url:
            self.discord_webhook_url = os.environ.get("CYRIS_DISCORD_WEBHOOK_URL", "")
        return self


class RoutingConfig(BaseModel):
    score_threshold: int = Field(default=70, ge=0, le=100)  # Featured article threshold
    summarize_score_threshold: int = Field(default=70, ge=0, le=100)


class GeneralConfig(BaseModel):
    # Grade D, file fallback: the effective schedule is the D1 settings row.
    # This is what a deployment falls back to when that row is absent.
    digest_schedule: list[str] = Field(default_factory=lambda: ["08:00", "20:00"])
    timezone: str = "Asia/Taipei"
    digest_window_hours: int = Field(default=24, ge=1, le=168)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)


class LLMProviderConfig(BaseModel):
    # No default provider — the user opts in explicitly. Unset ⇒ degraded
    # (excerpt-only) mode instead of silently defaulting to one vendor.
    provider: Literal["anthropic", "gemini", "openai", "workers_ai"] | None = None
    model: str = ""  # empty ⇒ the provider's default model (see bootstrap.build_llm)
    api_key: str = ""
    account_id: str = ""  # workers_ai only: its REST path is per-account

    @property
    def api_key_env_var(self) -> str:
        return {
            "gemini": "GEMINI_API_KEY",
            "openai": "OPENAI_API_KEY",
            "workers_ai": "CLOUDFLARE_AI_TOKEN",
        }.get(self.provider or "", "ANTHROPIC_API_KEY")

    @model_validator(mode="after")
    def inject_api_key(self) -> "LLMProviderConfig":
        if self.provider and not self.api_key:
            self.api_key = os.environ.get(self.api_key_env_var, "")
        if self.provider == "workers_ai":
            if not self.api_key:
                # The Workers AI token `cyris embed-compare` already uses: the same
                # "Workers AI -> Read" covers text models, so an existing setup needs
                # nothing new. Deliberately never CLOUDFLARE_API_TOKEN — that one
                # carries D1 and Pages, and would fail as a confusing 403 instead of
                # an obviously missing key.
                self.api_key = os.environ.get("CLOUDFLARE_EMBEDDING_API_TOKEN", "")
            if not self.account_id:
                self.account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
        return self


class DigestConfig(BaseModel):
    max_articles_per_digest: int = 200
    max_articles_per_digest_output: int = 15
    scoring_snippet_length: int = 1000
    summarize_snippet_length: int = 1000
    filter_snippet_length: int = Field(default=500, ge=1)
    output_language: str = "繁體中文"  # language for headlines/summaries in the digest
    style_prompt: str = ""  # optional reader-defined tone/focus injected into prompts


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
    agent_vault: AgentVaultConfig = Field(default_factory=AgentVaultConfig)
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


class Config(BaseModel):
    app: AppConfig
    sources: dict[str, SourceConfig]
    # Which one won: a D1-backed deployment silently falling back to the file is
    # exactly the kind of half-migration `cyris doctor` exists to surface.
    sources_origin: Literal["sources.yaml", "d1"] = "sources.yaml"

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


def write_llm_provider(config_path: Path, provider: str, model: str) -> None:
    """Point `[llm_provider]` at a different provider and model, in place.

    Deliberately a line edit rather than a TOML round-trip. Every writer worth
    depending on drops comments, and `cyris.toml` is mostly comments — the
    workers_ai caveat, which env var each provider reads, what the digest caps
    mean. Losing those to save a settings write would be a bad trade, and only
    two scalars in one known table ever change.

    Raises KeyError if the file has no `[llm_provider]` table to edit.
    """
    lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
    written = {"provider": False, "model": False}
    out: list[str] = []
    in_table = False

    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("["):
            # Leaving the table with a key still unwritten means the file simply
            # omitted it (both are optional), so append before moving on.
            if in_table:
                out.extend(
                    f'{key} = "{value}"\n'
                    for key, value in (("provider", provider), ("model", model))
                    if not written[key]
                )
                written = dict.fromkeys(written, True)
            in_table = stripped.startswith("[llm_provider]")
        elif in_table:
            for key, value in (("provider", provider), ("model", model)):
                if re.match(rf"{key}\s*=", stripped):
                    # Keep whatever trailing comment documented this line.
                    comment = line.partition("#")[2].rstrip("\n")
                    line = f'{key} = "{value}"' + (f"  #{comment}" if comment else "") + "\n"
                    written[key] = True
                    break
        out.append(line)

    if not in_table and not all(written.values()):
        raise KeyError(f"{config_path} has no [llm_provider] table to write to")
    out.extend(
        f'{key} = "{value}"\n'
        for key, value in (("provider", provider), ("model", model))
        if not written[key]
    )
    config_path.write_text("".join(out), encoding="utf-8")


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

    from_d1 = _sources_from_d1(app_config)
    return Config(
        app=app_config,
        sources=from_d1 or sources_dict,
        sources_origin="d1" if from_d1 else "sources.yaml",
    )


def _sources_from_d1(app_config: AppConfig) -> dict[str, SourceConfig] | None:
    """The `sources` table when D1 is on and populated, else None to use the file.

    Deliberately falls back rather than failing: a deployment that has switched
    the store to D1 but has not run `cyris sources push` yet must still fetch,
    and an unreachable D1 must not silently drop every source.
    """
    if not app_config.store.is_d1:
        return None

    from cyris.adapters.store.d1 import D1Client
    from cyris.adapters.store.source_store import D1SourceStore

    try:
        client = D1Client(
            account_id=app_config.store.account_id,
            database_id=app_config.store.database_id,
            api_token=app_config.store.api_token,
        )
        sources = D1SourceStore(client).list_sources()
    except Exception as e:  # noqa: BLE001 - any failure means "use the file"
        logger.warning("Could not read sources from D1 (%s); using sources.yaml", e)
        return None

    if not sources:
        logger.info("No sources in D1 yet; using sources.yaml. Run `cyris sources push`.")
        return None
    return sources
