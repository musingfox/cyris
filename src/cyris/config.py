"""Configuration loader for Cyris.

Loads cyris.toml (app config) and sources.yaml (source definitions).
Sensitive values are injected from environment variables.
"""

import logging
import os
import tomllib
import zoneinfo
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from cyris.domain.models import SourceConfig
from cyris.service_layer.schedule import validate_schedule


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


# Every grade-D setting (docs/architecture.md §5), as `table.field`. One list serves
# as the required set, the D1 whitelist and the /settings fields, so a key cannot be
# required without a writer or writable without a reader.
GRADE_D_KEYS: tuple[str, ...] = (
    "general.digest_schedule",
    "general.timezone",
    "general.digest_window_hours",
    "llm_provider.provider",
    "llm_provider.model",
    "notify.discord_webhook_url",
    "digest.max_articles_per_digest",
    "digest.max_articles_per_digest_output",
    "digest.max_featured",
    "digest.scoring_snippet_length",
    "digest.summarize_snippet_length",
    "digest.filter_snippet_length",
    "digest.output_language",
    "digest.style_prompt",
    "routing.score_threshold",
    "routing.summarize_score_threshold",
    "vote_similarity.enabled",
    "vote_similarity.provider",
    "vote_similarity.model",
    "vote_similarity.max_seeds",
)


class NotifyConfig(BaseModel):
    discord_webhook_url: str = ""  # "" ⇒ notifications off


def _known_timezone(name: str) -> str:
    try:
        zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"{name!r} is not a timezone this server knows") from None
    return name


def _not_blank(text: str) -> str:
    if not text.strip():
        raise ValueError("must not be empty")
    return text


Timezone = Annotated[str, AfterValidator(_known_timezone)]
DigestSchedule = Annotated[list[str], AfterValidator(validate_schedule)]
NonBlank = Annotated[str, AfterValidator(_not_blank)]


class RoutingConfig(BaseModel):
    score_threshold: int = Field(default=70, ge=0, le=100)  # Featured article threshold
    summarize_score_threshold: int = Field(default=70, ge=0, le=100)


class GeneralConfig(BaseModel):
    digest_schedule: DigestSchedule = Field(default_factory=lambda: ["08:00", "20:00"])
    timezone: Timezone = "Asia/Taipei"
    digest_window_hours: int = Field(default=24, ge=1, le=168)


class LLMProviderConfig(BaseModel):
    # No default provider — the user opts in explicitly. "none" ⇒ excerpt-only
    # by choice: no client is built and no key is needed.
    provider: Literal["anthropic", "gemini", "openai", "workers_ai", "none"] | None = None
    model: str = ""  # empty ⇒ the provider's default model (see bootstrap.build_llm)
    api_key: str = ""
    account_id: str = ""  # workers_ai only: its REST path is per-account

    @property
    def api_key_env_var(self) -> str:
        return {
            "gemini": "GEMINI_API_KEY",
            "openai": "OPENAI_API_KEY",
            "workers_ai": "CLOUDFLARE_AI_TOKEN",
            "none": "",
        }.get(self.provider or "", "ANTHROPIC_API_KEY")

    @model_validator(mode="after")
    def inject_api_key(self) -> "LLMProviderConfig":
        if self.provider in (None, "none"):
            return self
        if not self.api_key:
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
    max_articles_per_digest: int = Field(default=200, ge=1)
    max_articles_per_digest_output: int = Field(default=15, ge=1)
    # How many featured sections lead the page. A reader preference, not a
    # measurement — see docs/architecture.md §5.
    max_featured: int = Field(default=5, ge=1)
    scoring_snippet_length: int = Field(default=1000, ge=1)
    summarize_snippet_length: int = Field(default=1000, ge=1)
    filter_snippet_length: int = Field(default=500, ge=1)
    output_language: NonBlank = "zh-Hant"  # BCP 47 tag; service_layer/languages.json names it
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
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
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


_SETTINGS_TABLES: dict[str, type[BaseModel]] = {
    "general": GeneralConfig,
    "notify": NotifyConfig,
    "llm_provider": LLMProviderConfig,
    "digest": DigestConfig,
    "routing": RoutingConfig,
    "vote_similarity": VoteSimilarityConfig,
}


@cache
def _setting_adapter(key: str) -> TypeAdapter:
    # The field's own type and constraints, without its table: validating one key
    # must not need values for the others.
    table, field = key.split(".", 1)
    info = _SETTINGS_TABLES[table].model_fields[field]
    if not info.metadata:
        return TypeAdapter(info.annotation)
    return TypeAdapter(Annotated[(info.annotation, *info.metadata)])


def validate_setting(key: str, value: Any) -> Any:
    """The value a grade-D key would hold, or a ValueError saying why it cannot."""
    if key not in GRADE_D_KEYS:
        raise ValueError(f"not a settings key: {key}")
    if value is None:
        raise ValueError(f"{key} is required")
    try:
        return _setting_adapter(key).validate_python(value)
    except ValidationError as e:
        reasons = [err["msg"].removeprefix("Value error, ") for err in e.errors()]
        raise ValueError("; ".join(reasons)) from None


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


class IncompleteSettingsError(ValueError):
    """A command that reads runtime settings was started while some are missing."""


class Config(BaseModel):
    app: AppConfig
    sources: dict[str, SourceConfig]
    # Which one won: a D1-backed deployment silently falling back to the file is
    # exactly the kind of half-migration `cyris doctor` exists to surface.
    sources_origin: Literal["sources.yaml", "d1"] = "sources.yaml"
    # Grade-D keys this deployment's one home does not hold. Never raises at load:
    # the commands that fill the home have to start while it is empty.
    missing_settings: list[str] = Field(default_factory=list)
    # Each grade-D value the home does hold, validated on its own. Recorded apart
    # from the tables, which fill what is absent.
    settings_values: dict[str, Any] = Field(default_factory=dict)
    config_file_found: bool = True

    def present_settings(self) -> dict[str, Any]:
        """The grade-D values this deployment's home holds, by `table.field`."""
        return dict(self.settings_values)

    def missing_store_keys(self) -> list[str]:
        """Env vars a D1 store needs and does not have; empty when json or complete."""
        return _missing_store_keys(self.app)

    def require_complete_settings(self) -> None:
        """Raise IncompleteSettingsError naming every grade-D key the home lacks."""
        if not self.missing_settings:
            return
        keys = ", ".join(self.missing_settings)
        if self.app.store.is_d1:
            raise IncompleteSettingsError(
                f"Missing settings in D1: {keys}. Set them on /settings, or run "
                "`cyris settings push` to copy them from cyris.toml."
            )
        raise IncompleteSettingsError(
            f"Missing from cyris.toml: {keys}. cyris.toml.example lists every key."
        )

    def validate_required_keys(self) -> None:
        """Raise ValueError if required API keys are missing.

        The LLM is optional: provider "none" (or none configured) runs the
        excerpt-only digest, so only a real provider that is missing its key
        counts as an error.
        """
        missing = self.missing_store_keys()
        llm = self.app.llm_provider
        if llm.provider not in (None, "none") and not llm.api_key:
            missing.append(llm.api_key_env_var)
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")


@dataclass(frozen=True)
class RawConfig:
    """What the two files say, before any home for grade-D settings is chosen."""

    toml: dict[str, Any]
    sources: dict[str, SourceConfig]
    config_file_found: bool


def read_config_files(
    config_path: Path | None = None,
    sources_path: Path | None = None,
) -> RawConfig:
    """Read cyris.toml and sources.yaml, and load the `.env` beside the config.

    Raises:
        TOMLDecodeError: If the config file exists but is malformed.
        ValidationError: If sources.yaml is invalid.
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

    sources_config = SourcesConfig.model_validate(raw_yaml or {})

    defaults = sources_config.defaults
    for source in sources_config.sources:
        if source.language == "auto" and "language" in defaults:
            source.language = defaults["language"]

    return RawConfig(
        toml=raw_toml,
        sources={s.name: s for s in sources_config.sources},
        config_file_found=config_file_found,
    )


def resolve_config(raw: RawConfig, d1_settings: dict[str, Any] | None = None) -> Config:
    """The Config the files describe, with grade-D settings from exactly one home.

    `d1_settings` None makes cyris.toml the home: an invalid value there fails the
    load, because the file is fixed in an editor. Otherwise the D1 rows are the
    home and the file's grade-D keys are ignored; a row that fails its rule counts
    as missing, because D1 is fixed through /settings, which has to start.
    """
    if d1_settings is None:
        app_config = AppConfig.model_validate(raw.toml)
        settings_values, missing_settings = _file_settings(raw.toml)
    else:
        settings_values, missing_settings = _stored_settings(d1_settings)
        app_config = AppConfig.model_validate(_with_settings(raw.toml, settings_values))
    return Config(
        app=app_config,
        sources=raw.sources,
        missing_settings=missing_settings,
        settings_values=settings_values,
        config_file_found=raw.config_file_found,
    )


def load_config(
    config_path: Path | None = None,
    sources_path: Path | None = None,
) -> Config:
    """Load and validate configuration from TOML and YAML files.

    The files alone: cyris.toml is the home of every grade-D setting here and
    sources.yaml of the sources. A D1 deployment is resolved by
    `bootstrap.load_effective_config`, which reads both from D1 instead.

    Args:
        config_path: Path to cyris.toml. Defaults to ./cyris.toml.
        sources_path: Path to sources.yaml. Defaults to ./sources.yaml.

    Returns:
        Validated Config object.

    Raises:
        TOMLDecodeError: If the config file exists but is malformed.
        ValueError: If a value is invalid (pydantic's ValidationError is one).
    """
    return resolve_config(read_config_files(config_path, sources_path))


def _file_settings(raw_toml: dict) -> tuple[dict[str, Any], list[str]]:
    """The grade-D values a cyris.toml sets, validated, and the keys it leaves out."""
    values: dict[str, Any] = {}
    missing: list[str] = []
    for key in GRADE_D_KEYS:
        table, field = key.split(".", 1)
        body = raw_toml.get(table)
        if isinstance(body, dict) and field in body:
            values[key] = validate_setting(key, body[field])
        else:
            missing.append(key)
    return values, sorted(missing)


def _stored_settings(stored: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The grade-D rows that pass their rule, and the keys without one that does."""
    values: dict[str, Any] = {}
    missing: list[str] = []
    for key in GRADE_D_KEYS:
        if key not in stored:
            missing.append(key)
            continue
        try:
            values[key] = validate_setting(key, stored[key])
        except ValueError as e:
            logger.warning("Ignoring D1 setting %s: %s", key, e)
            missing.append(key)
    return values, sorted(missing)


def _with_settings(raw_toml: dict, values: dict[str, Any]) -> dict[str, Any]:
    """The file with its grade-D keys replaced by `values`, key by key.

    Key level, not table level: `[llm_provider] api_key` and `[vote_similarity]
    threshold` share a table with grade-D keys and still come from the file.
    """
    merged: dict[str, Any] = {}
    for table, body in raw_toml.items():
        if isinstance(body, dict):
            body = {k: v for k, v in body.items() if f"{table}.{k}" not in GRADE_D_KEYS}
        merged[table] = body
    for key, value in values.items():
        table, field = key.split(".", 1)
        if not isinstance(merged.get(table), dict):
            merged[table] = {}
        merged[table][field] = value
    return merged
