"""Configuration loader for Cyris.

Loads cyris.toml (app config) and sources.yaml (source definitions).
Sensitive values are injected from environment variables.
"""

import logging
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


logger = logging.getLogger(__name__)

# Grade-B deployment identity. The environment supplies a key the file left
# absent or empty; a set file value always wins. Named CYRIS_<TABLE>_<KEY>.
B_GRADE_ENV_VARS: dict[str, str] = {
    "store.backend": "CYRIS_STORE_BACKEND",
    "store.database_id": "CYRIS_STORE_DATABASE_ID",
    "html_output.enabled": "CYRIS_HTML_OUTPUT_ENABLED",
    "promote.publish_enabled": "CYRIS_PROMOTE_PUBLISH_ENABLED",
    "promote.pages_project": "CYRIS_PROMOTE_PAGES_PROJECT",
    "promote.custom_domain": "CYRIS_PROMOTE_CUSTOM_DOMAIN",
    "promote.worker_url": "CYRIS_PROMOTE_WORKER_URL",
    "newsletter.worker_url": "CYRIS_NEWSLETTER_WORKER_URL",
    "rss.worker_url": "CYRIS_RSS_WORKER_URL",
}


def _fill_from_env(data: object, fields: dict[str, str]) -> object:
    data = {} if not isinstance(data, dict) else dict(data)
    for field, env_name in fields.items():
        env_val = os.environ.get(env_name)
        # An exported-but-empty key is *unset*, not a value: `cp .env.example .env`
        # exports every one of these blank, and "" is not a valid backend or bool.
        if not env_val:
            continue
        if field not in data or data[field] == "":
            data[field] = env_val
    return data


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
                # carries D1 and Pages but answers 401 on Workers AI, which reads as
                # a broken key rather than a missing permission.
                self.api_key = os.environ.get("CLOUDFLARE_EMBEDDING_API_TOKEN", "")
            if not self.account_id:
                self.account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
        return self


class DigestConfig(BaseModel):
    max_articles_per_digest: int = 200
    max_articles_per_digest_output: int = 15
    # How many featured sections lead the page. A reader preference, not a
    # measurement — see docs/architecture.md §5.
    max_featured: int = Field(default=5, ge=1)
    scoring_snippet_length: int = 1000
    summarize_snippet_length: int = 1000
    filter_snippet_length: int = Field(default=500, ge=1)
    output_language: str = "zh-Hant"  # BCP 47 tag; service_layer/languages.json names it
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

    @model_validator(mode="before")
    @classmethod
    def inject_b_grade(cls, data: object) -> object:
        return _fill_from_env(
            data,
            {
                "backend": B_GRADE_ENV_VARS["store.backend"],
                "database_id": B_GRADE_ENV_VARS["store.database_id"],
            },
        )

    @model_validator(mode="after")
    def inject_credentials(self) -> "StoreConfig":
        if not self.account_id:
            self.account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
        if not self.api_token:
            self.api_token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
        return self

    @property
    def is_d1(self) -> bool:
        return self.backend == "d1"


class HtmlOutputConfig(BaseModel):
    enabled: bool = False
    output_dir: str = "agent-vault/html"

    @model_validator(mode="before")
    @classmethod
    def inject_b_grade(cls, data: object) -> object:
        return _fill_from_env(data, {"enabled": B_GRADE_ENV_VARS["html_output.enabled"]})


class WorkerConfig(BaseModel):
    """A Cloudflare Worker cyris pulls from, and the bearer it presents.

    One token for the two that are server-to-server (`rss`, `newsletter`).
    They were separate random values but never separate trust domains: the same
    `.env` and the same Worker secret store hold both, so whoever reads one
    reads the other.

    `promote` is **not** one of them — see PromoteConfig.
    """

    worker_url: str = ""
    token: str = ""

    @model_validator(mode="after")
    def inject_token(self) -> "WorkerConfig":
        if not self.token:
            self.token = os.environ.get("CYRIS_WORKER_TOKEN", "")
        return self


class PromoteConfig(WorkerConfig):
    """The vote Worker. Its token is **server-side only** (since private-votes-public-archive).

    Votes go through `POST /api/vote` on the Worker (Access-only, no UI token),
    which attaches `CYRIS_PROMOTE_TOKEN` server-side and forwards to the promote
    Worker. The token is never rendered into HTML anymore.

    It remains separate from `CYRIS_WORKER_TOKEN` (used by `rss` and `newsletter`)
    because their access patterns differ: votes are Access-gated, while the other
    Workers use server-to-server bearer auth. The dividing line is Access vs bearer.
    """

    publish_enabled: bool = False
    pages_project: str = ""
    custom_domain: str = ""  # Custom domain for operator/self links (e.g., digest.musingfox.me)

    @model_validator(mode="before")
    @classmethod
    def inject_b_grade(cls, data: object) -> object:
        return _fill_from_env(
            data,
            {
                "publish_enabled": B_GRADE_ENV_VARS["promote.publish_enabled"],
                "pages_project": B_GRADE_ENV_VARS["promote.pages_project"],
                "custom_domain": B_GRADE_ENV_VARS["promote.custom_domain"],
                "worker_url": B_GRADE_ENV_VARS["promote.worker_url"],
            },
        )

    @model_validator(mode="after")
    def inject_token(self) -> "PromoteConfig":
        if not self.token:
            self.token = os.environ.get("CYRIS_PROMOTE_TOKEN", "")
        return self


class NewsletterConfig(WorkerConfig):
    @model_validator(mode="before")
    @classmethod
    def inject_b_grade(cls, data: object) -> object:
        return _fill_from_env(data, {"worker_url": B_GRADE_ENV_VARS["newsletter.worker_url"]})


class VoteSimilarityConfig(BaseModel):
    """Suppress candidates that sit close to what the reader downvoted.

    Off by default: it changes what reaches the digest, and the threshold was
    calibrated on one reader's votes. See docs/vote-signal-measurement.md.
    """

    enabled: bool = False
    provider: Literal["workers_ai", "gemini"] = "workers_ai"
    # None means "the provider's own calibration". Grade A: the pairing is a
    # measured property of the model, not a preference — bge-m3's cosines run
    # lower than Gemini's across the board, so carrying 0.68 over to it would
    # suppress nothing and the feature would silently no-op.
    threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    model: str = ""
    max_seeds: int = Field(default=200, ge=1)


class RssConfig(WorkerConfig):
    """Cloudflare RSS Worker — the hourly feed buffer the pipeline reads from."""

    @model_validator(mode="before")
    @classmethod
    def inject_b_grade(cls, data: object) -> object:
        return _fill_from_env(data, {"worker_url": B_GRADE_ENV_VARS["rss.worker_url"]})


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


def _missing_store_keys(app_config: "AppConfig") -> list[str]:
    """Env vars a D1 store needs and does not have; empty when json or complete."""
    if not app_config.store.is_d1:
        return []
    missing = []
    if not app_config.store.database_id:
        missing.append("CYRIS_STORE_DATABASE_ID")
    if not app_config.store.account_id:
        missing.append("CLOUDFLARE_ACCOUNT_ID")
    if not app_config.store.api_token:
        missing.append("CLOUDFLARE_API_TOKEN")
    return missing


class Config(BaseModel):
    app: AppConfig
    sources: dict[str, SourceConfig]
    # Which one won: a D1-backed deployment silently falling back to the file is
    # exactly the kind of half-migration `cyris doctor` exists to surface.
    sources_origin: Literal["sources.yaml", "d1"] = "sources.yaml"
    # Grade-D keys this run took from the D1 `settings` table rather than the
    # file. Same reason as above: which home won has to be reportable.
    settings_from_d1: list[str] = Field(default_factory=list)
    config_file_found: bool = True

    def missing_store_keys(self) -> list[str]:
        """Env vars a D1 store needs and does not have; empty when json or complete."""
        return _missing_store_keys(self.app)

    def validate_required_keys(self) -> None:
        """Raise ValueError if required API keys are missing.

        The LLM is optional: with no provider configured the pipeline runs in
        degraded (excerpt-only) mode, so only a provider that IS set but is
        missing its key counts as an error.
        """
        missing = self.missing_store_keys()
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
        TOMLDecodeError: If the config file exists but is malformed.
        ValueError: If required env vars are missing.
        ValidationError: If config structure is invalid.
    """
    config_path = config_path or Path("cyris.toml")
    sources_path = sources_path or Path("sources.yaml")

    _load_dotenv(config_path.parent / ".env")

    config_file_found = config_path.exists()
    if config_file_found:
        with open(config_path, "rb") as f:
            raw_toml = tomllib.load(f)
    else:
        logger.warning("Config file not found: %s", config_path)
        raw_toml = {}

    if sources_path.exists():
        with open(sources_path) as f:
            raw_yaml = yaml.safe_load(f)
    else:
        logger.warning("Sources file not found: %s", sources_path)
        raw_yaml = {}

    app_config = AppConfig.model_validate(raw_toml)

    sources_config = SourcesConfig.model_validate(raw_yaml or {})

    defaults = sources_config.defaults
    for source in sources_config.sources:
        if source.language == "auto" and "language" in defaults:
            source.language = defaults["language"]

    sources_dict = {s.name: s for s in sources_config.sources}

    from_d1 = _sources_from_d1(app_config)
    return Config(
        app=app_config,
        sources=from_d1 or sources_dict,
        sources_origin="d1" if from_d1 else "sources.yaml",
        config_file_found=config_file_found,
    )


def _sources_from_d1(app_config: AppConfig) -> dict[str, SourceConfig] | None:
    """The `sources` table when D1 is on and populated, else None to use the file.

    Deliberately falls back rather than failing: a deployment that has switched
    the store to D1 but has not run `cyris sources push` yet must still fetch,
    and an unreachable D1 must not silently drop every source.
    """
    if not app_config.store.is_d1:
        return None

    missing = _missing_store_keys(app_config)
    if missing:
        # Four doomed retries ahead of the error that names these is 13 seconds
        # of noise for a first deploy; the file fallback is the same either way.
        logger.warning("D1 store is missing %s; using sources.yaml", ", ".join(missing))
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
