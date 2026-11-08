"""Configuration loader for Cyris.

Loads cyris.toml (app config) and sources.yaml (source definitions).
Sensitive values are injected from environment variables.
"""

import json
import logging
import os
import re
import tomllib
import zoneinfo
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    Field,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from cyris.domain.areas import Area
from cyris.domain.models import SourceConfig, TrackedTopic
from cyris.service_layer.schedule import validate_schedule


def _load_dotenv(env_path: Path | None = None) -> frozenset[str]:
    """Load .env file into os.environ (setdefault, won't override existing).

    Returns the names it set, which is how `cyris config show` tells a value
    from `.env` from one the process environment already held.
    """
    path = env_path or Path(".env")
    if not path.exists():
        return frozenset()
    loaded = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key not in os.environ:
            loaded.add(key)
        os.environ.setdefault(key, value.strip())
    return frozenset(loaded)


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


def exists(path: Path) -> bool:
    """Whether `path` is there to see; a path under a directory this user cannot traverse is not."""
    try:
        return path.exists()
    except OSError:
        return False


def can_create(path: Path) -> bool:
    """Whether this user could create `path`: its nearest existing parent is theirs to write."""
    nearest = next(parent for parent in path.parents if exists(parent))
    return nearest.is_dir() and os.access(nearest, os.W_OK | os.X_OK)


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


# Grade-B identity only a local install has. The Worker forwards `B_GRADE_ENV_VARS` into the
# container, and a deployment it runs publishes and keeps its state in D1, so it has no use
# for these.
LOCAL_B_GRADE_ENV_VARS: dict[str, str] = {
    "html_output.base_url": "CYRIS_HTML_OUTPUT_BASE_URL",
    "store.sqlite_path": "CYRIS_STORE_SQLITE_PATH",
}


# Every grade-D setting (docs/architecture.md §5), as `table.field`, with the
# /settings field that edits it: its category, label, controls and save route.
# One registry is the settings set, the D1 whitelist and the page's fields, so a
# key cannot be required without a writer or writable without a reader. An
# `"optional": true` entry is the exception to required: see OPTIONAL_SETTINGS.
SETTINGS_FIELDS: dict[str, dict[str, Any]] = json.loads(
    (files("cyris") / "settings_fields.json").read_text(encoding="utf-8")
)
GRADE_D_KEYS: tuple[str, ...] = tuple(SETTINGS_FIELDS)


class NotifyConfig(BaseModel):
    discord_webhook_url: str  # "" ⇒ notifications off
    email_to: str  # "" ⇒ no mail
    email_from: str


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
    score_threshold: int = Field(ge=0, le=100)  # Two of a group at this score make it the Top story


class GeneralConfig(BaseModel):
    digest_schedule: DigestSchedule
    timezone: Timezone
    digest_window_hours: int = Field(ge=1, le=168)


ReasoningEffort = Literal["default", "low", "medium", "high"]


class LLMProviderConfig(BaseModel):
    # "none" ⇒ excerpt-only by choice: no client is built and no key is needed.
    provider: Literal["anthropic", "gemini", "openai", "workers_ai", "ai_gateway", "none"]
    model: str  # empty ⇒ the provider's default model (see bootstrap.build_llm)
    # "default" ⇒ each provider's own behaviour before this setting existed.
    reasoning_effort: ReasoningEffort = "default"
    api_key: str = ""
    account_id: str = ""  # workers_ai and ai_gateway only: their REST paths are per-account

    @property
    def chooses_no_llm(self) -> bool:
        return self.provider == "none"

    @property
    def api_key_env_var(self) -> str:
        return {
            "gemini": "GEMINI_API_KEY",
            "openai": "OPENAI_API_KEY",
            "workers_ai": "CLOUDFLARE_AI_TOKEN",
            # The gateway's own Run permission, not Workers AI: the embedding token is 401 there.
            "ai_gateway": "CLOUDFLARE_AIG_TOKEN",
            "none": "",
        }.get(self.provider, "ANTHROPIC_API_KEY")

    @model_validator(mode="after")
    def inject_api_key(self) -> "LLMProviderConfig":
        if self.provider == "none":
            return self
        if not self.api_key:
            self.api_key = os.environ.get(self.api_key_env_var, "")
        if self.provider in ("workers_ai", "ai_gateway"):
            if not self.api_key and self.provider == "workers_ai":
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
    max_articles_per_digest: int = Field(ge=1)
    scoring_snippet_length: int = Field(ge=1)
    summarize_snippet_length: int = Field(ge=1)
    filter_snippet_length: int = Field(ge=1)
    output_language: NonBlank  # BCP 47 tag; service_layer/languages.json names it
    style_prompt: str  # reader-defined tone/focus injected into prompts; "" ⇒ none
    # Every page's --type-scale. The app Worker reads it and injects it into each
    # HTML page it serves (workers/app/src/type_scale.js holds the same steps).
    type_scale: Literal[0.875, 1, 1.125]
    # Order the headlines and Features by the reader's topic votes; false keeps the
    # model's order. See docs/architecture.md §3.2.
    rank_by_preference: bool
    # Which judge scores the vote order: cosine, a stored name kept, is the topic order
    # (ADR-0023); clef is the Workers AI model, and an issue it cannot fully score
    # falls back to the topic order.
    preference_source: Literal["cosine", "clef"]
    # Whether follow-lane articles get the read lane's explanation; false keeps their excerpt.
    summarize_follow: bool
    # Option I's two clocks (ADR-0023): a topic vote halves every `vote_half_life_days`,
    # and the topics last seen within `topic_lifetime_days` are the recent ones.
    vote_half_life_days: int = Field(ge=1, le=365)
    topic_lifetime_days: int = Field(ge=1, le=365)


class AgentVaultConfig(BaseModel):
    path: Path = Path("./agent-vault")

    @model_validator(mode="after")
    def override_path(self) -> "AgentVaultConfig":
        if p := os.environ.get("CYRIS_AGENT_VAULT_PATH"):
            self.path = Path(p)
        return self


class StoreConfig(BaseModel):
    """Where persistent state lives.

    `sqlite`, the default, keeps every table in one SQLite file on this machine,
    a local install's home (ADR-0025). `d1` puts the same tables in Cloudflare D1,
    so a dead local machine loses nothing. Each is a whole home, never overlaid on
    the other (ADR-0003).
    """

    backend: Literal["d1", "sqlite"] = "sqlite"
    database_id: str = ""
    account_id: str = ""
    api_token: str = ""
    # Relative to the directory of cyris.toml, as `.env` is, and to the working directory
    # with no file: `resolve_config` anchors it. Never inside the vault: compose bind-mounts
    # it, and a host process and a container on one SQLite file corrupt it (ADR-0025).
    # AppConfig refuses such a path.
    sqlite_path: Path = Path("state/cyris.sqlite3")

    @model_validator(mode="before")
    @classmethod
    def inject_b_grade(cls, data: object) -> object:
        return _fill_from_env(
            data,
            {
                "backend": B_GRADE_ENV_VARS["store.backend"],
                "database_id": B_GRADE_ENV_VARS["store.database_id"],
                "sqlite_path": LOCAL_B_GRADE_ENV_VARS["store.sqlite_path"],
            },
        )

    @field_validator("sqlite_path", mode="before")
    @classmethod
    def blank_path_is_the_default(cls, value: object) -> object:
        # `sqlite_path = ""` is unset, as an empty grade-B value is everywhere else.
        return cls.model_fields["sqlite_path"].default if value == "" else value

    @model_validator(mode="after")
    def inject_credentials(self) -> "StoreConfig":
        if not self.account_id:
            self.account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
        if not self.api_token:
            self.api_token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
        return self

    @property
    def is_cloudflare(self) -> bool:
        """The tables are Cloudflare D1, reached over REST with Cloudflare credentials."""
        return self.backend == "d1"

    @property
    def sqlite_not_created(self) -> bool:
        """A local install's file does not exist yet, and opening it would create it.

        Not one that cannot be created: opening that names the reason.
        """
        return (
            self.backend == "sqlite"
            and not exists(self.sqlite_path)
            and can_create(self.sqlite_path)
        )

    @property
    def sql_home(self) -> str:
        """The SQL store as a reader names it: `D1`, or the local file."""
        return "D1" if self.is_cloudflare else "the SQLite store"


BASE_URL = re.compile(r"https?://(?:[A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9A-Fa-f:.]+\])(?::\d+)?/?")


class HtmlOutputConfig(BaseModel):
    enabled: bool = False
    output_dir: str = "agent-vault/html"
    # Where a reader opens these pages, for the notification's Read online link when nothing
    # publishes them. Empty sends no link: localhost is wrong on every other device, so
    # nothing here can guess it. A published issue's link comes from `[promote]` instead.
    base_url: str = ""

    @model_validator(mode="before")
    @classmethod
    def inject_b_grade(cls, data: object) -> object:
        return _fill_from_env(
            data,
            {
                "enabled": B_GRADE_ENV_VARS["html_output.enabled"],
                "base_url": LOCAL_B_GRADE_ENV_VARS["html_output.base_url"],
            },
        )

    @field_validator("base_url")
    @classmethod
    def base_url_is_an_address(cls, value: str) -> str:
        if not value:
            return value
        # Scheme, host and port only: the mail and onboarding build further links from the
        # host alone, so a path would be dropped from those and a query would swallow the slug.
        if not BASE_URL.fullmatch(value):
            raise ValueError(f"[html_output] base_url must be http(s)://host[:port], got {value!r}")
        urlsplit(value).port  # noqa: B018 - raises ValueError out of range
        return value.rstrip("/")


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
    """The vote Worker. Its token is **server-side only** (since the private-votes change).

    Votes go through `POST /api/vote` on the Worker (Access-only, no UI token),
    which attaches `CYRIS_PROMOTE_TOKEN` server-side and forwards to the promote
    Worker. The token is never rendered into HTML anymore.

    It remains separate from `CYRIS_WORKER_TOKEN` (used by `rss` and `newsletter`)
    because their access patterns differ: votes are Access-gated, while the other
    Workers use server-to-server bearer auth. The dividing line is Access vs bearer.
    """

    publish_enabled: bool = False
    pages_project: str = ""
    custom_domain: str = ""  # Custom domain for operator/self links (e.g., digest.example.com)

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
    """Suppress candidates that are outliers close to what the reader downvoted.

    It changes what reaches the digest. See
    docs/decisions/0017-suppression-is-a-relative-outlier-with-a-reason-and-a-clock.md.
    """

    enabled: bool
    provider: Literal["workers_ai", "gemini"]
    # Ignored since ADR-0017. Still parsed so a cyris.toml that sets it loads; doctor
    # warns that it is ignored.
    threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    model: str
    max_seeds: int = Field(ge=1)


class RssConfig(WorkerConfig):
    """Cloudflare RSS Worker — the hourly feed buffer the pipeline reads from."""

    @model_validator(mode="before")
    @classmethod
    def inject_b_grade(cls, data: object) -> object:
        return _fill_from_env(data, {"worker_url": B_GRADE_ENV_VARS["rss.worker_url"]})


class AppConfig(BaseModel):
    # The six tables holding grade-D keys are None until their home holds every
    # required one of those keys: a runtime setting has no value in code, except
    # an optional key, whose field default is a value it can store.
    general: GeneralConfig | None
    notify: NotifyConfig | None
    llm_provider: LLMProviderConfig | None
    digest: DigestConfig | None
    agent_vault: AgentVaultConfig = Field(default_factory=AgentVaultConfig)
    routing: RoutingConfig | None
    html_output: HtmlOutputConfig = Field(default_factory=HtmlOutputConfig)
    promote: PromoteConfig = Field(default_factory=PromoteConfig)
    newsletter: NewsletterConfig = Field(default_factory=NewsletterConfig)
    rss: RssConfig = Field(default_factory=RssConfig)
    store: StoreConfig = Field(default_factory=StoreConfig)
    vote_similarity: VoteSimilarityConfig | None

    # Before the vault check below, which has to ask about the file that will be opened.
    @model_validator(mode="after")
    def anchor_sqlite_path(self, info: ValidationInfo) -> "AppConfig":
        config_dir = (info.context or {}).get("config_dir")
        if config_dir is not None:
            self.store.sqlite_path = config_dir / self.store.sqlite_path
        return self

    @model_validator(mode="after")
    def sqlite_file_outside_the_vault(self) -> "AppConfig":
        if self.store.backend != "sqlite" or not _within(
            self.store.sqlite_path, self.agent_vault.path
        ):
            return self
        raise ValueError(
            f"[store] sqlite_path {self.store.sqlite_path} is inside the agent vault "
            f"{self.agent_vault.path}: docker compose bind-mounts the vault, and a host "
            "process and a container on one SQLite file corrupt it. Set [store] sqlite_path "
            "to a file outside the vault."
        )


def _within(path: Path, directory: Path) -> bool:
    """Whether `path` lies in `directory`, by the directory's identity on disk.

    A string comparison misses a spelling the filesystem treats as the same directory:
    `AGENT-VAULT/` on a case-insensitive APFS, or another Unicode normalization of the
    name. So each existing parent of the resolved path is compared with the directory
    by device and inode, which also covers a symlink and `..`. A directory that does not
    exist yet has no identity, and is compared by its resolved path alone.
    """
    path, directory = path.resolve(), directory.resolve()
    if path.is_relative_to(directory):
        return True
    try:
        target = directory.stat()
    except OSError:
        return False
    for parent in (path, *path.parents):
        try:
            found = parent.stat()
        except OSError:
            continue
        if (found.st_dev, found.st_ino) == (target.st_dev, target.st_ino):
            return True
    return False


_SETTINGS_TABLES: dict[str, type[BaseModel]] = {
    "general": GeneralConfig,
    "notify": NotifyConfig,
    "llm_provider": LLMProviderConfig,
    "digest": DigestConfig,
    "routing": RoutingConfig,
    "vote_similarity": VoteSimilarityConfig,
}

# The grade-D keys a home may leave out: the registry marks them and the table
# field's own default is the value they run on. Every other key is required.
OPTIONAL_SETTINGS: dict[str, Any] = {
    key: _SETTINGS_TABLES[table].model_fields[field].default
    for key, entry in SETTINGS_FIELDS.items()
    if entry.get("optional")
    for table, field in [key.split(".", 1)]
}
REQUIRED_KEYS: tuple[str, ...] = tuple(key for key in GRADE_D_KEYS if key not in OPTIONAL_SETTINGS)

# What the onboarding wizard's Model step shows a required key at while the store lacks
# it: the tuned values a new reader is not asked to choose, and vote similarity's switch,
# which the vote route saves with them (ADR-0025). The reader confirms them with Next,
# which writes them into the store once. Nothing reads them as a fallback: a key still
# missing on a running deployment stays an error (ADR-0003). `settings.example.toml` holds
# the same values, and `tests/test_settings_starting_values.py` keeps the two equal.
STARTING_VALUES: dict[str, Any] = json.loads(
    (files("cyris") / "settings_starting_values.json").read_text(encoding="utf-8")
)


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
        raise ValueError("a value is required")
    try:
        return _setting_adapter(key).validate_python(value)
    except ValidationError as e:
        reasons = [err["msg"].removeprefix("Value error, ") for err in e.errors()]
        raise ValueError("; ".join(reasons)) from None


class SourcesConfig(BaseModel):
    defaults: dict[str, str] = Field(default_factory=dict)
    sources: list[SourceConfig] = Field(default_factory=list)


def _missing_store_keys(app_config: "AppConfig") -> list[str]:
    """Env vars a Cloudflare D1 store needs and does not have; empty otherwise."""
    if not app_config.store.is_cloudflare:
        return []
    missing = []
    if not app_config.store.database_id:
        missing.append("CYRIS_STORE_DATABASE_ID")
    if not app_config.store.account_id:
        missing.append("CLOUDFLARE_ACCOUNT_ID")
    if not app_config.store.api_token:
        missing.append("CLOUDFLARE_API_TOKEN")
    return missing


class JsonStoreRemovedError(ValueError):
    """A config or environment still names the removed `json` backend."""


class IncompleteSettingsError(ValueError):
    """A command that reads runtime settings was started while some are missing."""


class NoSourcesError(ValueError):
    """A run was started with sources, none of which can be fetched from.

    An *empty* list is not this error: a deployment ships with no source and
    waits for onboarding to pick some (`Config.awaiting_onboarding`).
    """


class Config(BaseModel):
    app: AppConfig
    sources: dict[str, SourceConfig]
    # Optional, unlike every required grade-D key: an empty list tracks nothing and stops
    # nothing. From the store's `tracked_topics` table.
    tracked_topics: list[TrackedTopic] = Field(default_factory=list)
    # From the store's `areas` table.
    areas: list[Area] = Field(default_factory=list)
    # Grade-D keys this deployment's one home does not hold. Never raises at load:
    # the commands that fill the home have to start while it is empty.
    missing_settings: list[str] = Field(default_factory=list)
    # Each grade-D value the home does hold, validated on its own. Recorded apart
    # from the tables, which fill what is absent.
    settings_values: dict[str, Any] = Field(default_factory=dict)
    config_file_found: bool = True
    # Provenance only, for `cyris config show`: what cyris.toml said, and which
    # environment names the `.env` beside it supplied. No value is read from here.
    file_toml: dict[str, Any] = Field(default_factory=dict)
    dotenv_names: frozenset[str] = frozenset()

    def present_settings(self) -> dict[str, Any]:
        """The grade-D values this deployment's home holds, by `table.field`."""
        return dict(self.settings_values)

    def defaulted_settings(self) -> dict[str, Any]:
        """The optional keys the home does not hold, each with the value it runs on."""
        return {
            key: default
            for key, default in OPTIONAL_SETTINGS.items()
            if key not in self.settings_values and key not in self.missing_settings
        }

    def missing_store_keys(self) -> list[str]:
        """Env vars a Cloudflare D1 store needs and does not have; empty otherwise."""
        return _missing_store_keys(self.app)

    def require_complete_settings(self) -> None:
        """Raise IncompleteSettingsError naming every grade-D key the home lacks."""
        if not self.missing_settings:
            return
        keys = ", ".join(self.missing_settings)
        if self.app.store.is_cloudflare:
            raise IncompleteSettingsError(
                f"Missing settings in D1: {keys}. Set them on /settings, or run "
                "`cyris settings push --config <file>` to copy them from a settings file "
                "(settings.example.toml lists every key)."
            )
        # Not `settings push`: it writes to Cloudflare D1, never to this file.
        raise IncompleteSettingsError(
            f"Missing settings in {self.app.store.sql_home}: {keys}. Set them on /settings."
        )

    def awaiting_onboarding(self) -> bool:
        """No source at all: a deployment whose reader has not picked feeds yet.

        A permitted state, not an error. A new deployment's `sources` is empty
        until onboarding fills it, so a run ends there having fetched nothing
        and called no LLM, and a scheduled tick reports no failure.
        """
        return not self.sources

    def onboarding_wait(self) -> str:
        """Why a run with no source does nothing, and where a source comes from."""
        if self.app.store.is_cloudflare:
            where = "Add one on /settings, or run `cyris sources push`."
        else:
            # Not `sources push`: it writes to Cloudflare D1, never to this file.
            where = "Add one on /onboarding or /settings."
        return f"No sources yet, so this run has nothing to fetch: waiting for onboarding. {where}"

    def require_sources(self) -> None:
        """Raise NoSourcesError when every source in the one list is unfetchable.

        An empty list returns: that is `awaiting_onboarding`, which the caller
        reports instead of failing.
        """
        if not self.sources or any(s.fetchable for s in self.sources.values()):
            return
        names = ", ".join(sorted(self.sources))
        raise NoSourcesError(
            f"No fetchable sources in {self.app.store.sql_home}: {names}. "
            "An RSS source needs a feed URL, "
            "a newsletter source a sender match."
        )

    def validate_required_keys(self) -> None:
        """Raise ValueError if required API keys are missing.

        The LLM is optional: provider "none" runs the excerpt-only digest, so only
        a real provider that is missing its key counts as an error. An unbuilt
        table is the settings gate's to report.
        """
        missing = self.missing_store_keys()
        llm = self.app.llm_provider
        if llm is not None and llm.provider != "none" and not llm.api_key:
            missing.append(llm.api_key_env_var)
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")


@dataclass(frozen=True)
class RawConfig:
    """What the two files say, before the store is read."""

    toml: dict[str, Any]
    sources: dict[str, SourceConfig]
    config_file_found: bool
    dotenv_names: frozenset[str] = frozenset()
    # Where a relative `[store] sqlite_path` starts: the file's directory. None with no file.
    config_dir: Path | None = None


def read_config_files(
    config_path: Path | None = None,
    sources_path: Path | None = None,
) -> RawConfig:
    """Read cyris.toml and sources.yaml, and load the `.env` beside the config.

    Raises:
        TOMLDecodeError: If the config file exists but is malformed.
        JsonStoreRemovedError: If the file or the environment names the `json` backend.
        ValidationError: If sources.yaml is invalid.
    """
    config_path = config_path or Path("cyris.toml")
    sources_path = sources_path or Path("sources.yaml")

    dotenv_names = _load_dotenv(config_path.parent / ".env")

    config_file_found = config_path.exists()
    if config_file_found:
        with open(config_path, "rb") as f:
            raw_toml = tomllib.load(f)
    else:
        raw_toml = {}
    # Before anything else can be said about the file, so the refusal is the only line.
    _refuse_json_store(raw_toml, config_path)
    if not config_file_found:
        logger.warning("Config file not found: %s", config_path)

    if sources_path.exists():
        with open(sources_path) as f:
            raw_yaml = yaml.safe_load(f)
    else:
        # Not a warning: only `sources push` needs this file, and it reads it itself.
        logger.debug("Sources file not found: %s", sources_path)
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
        dotenv_names=dotenv_names,
        config_dir=config_path.parent if config_file_found else None,
    )


def _refuse_json_store(raw_toml: dict[str, Any], config_path: Path) -> None:
    """Stop the load, on one line, when the file or the environment names the `json` backend.

    Before pydantic, whose refusal of a value outside the `Literal` is several lines
    that name no way forward. Read as `StoreConfig` reads it: a set file value wins.
    """
    env_name = B_GRADE_ENV_VARS["store.backend"]
    named = _fill_from_env(raw_toml.get("store"), {"backend": env_name})
    if named.get("backend") != "json":
        return
    store = raw_toml.get("store")
    in_file = isinstance(store, dict) and store.get("backend") == "json"
    line = f'backend = "json" line from {config_path}' if in_file else f"{env_name}=json line"
    raise JsonStoreRemovedError(f"The json store was removed: delete the {line} to run on SQLite.")


def resolve_config(raw: RawConfig, d1_settings: dict[str, Any]) -> Config:
    """The Config the files describe, with grade-D settings from the store alone.

    The file's grade-D keys are ignored. A row in `d1_settings` that fails its rule
    counts as missing, because the store is fixed through /settings, which has to start.
    """
    settings_values, missing_settings = _stored_settings(d1_settings)
    app_config = AppConfig.model_validate(
        _with_settings(raw.toml, settings_values, missing_settings),
        context={"config_dir": raw.config_dir},
    )
    return Config(
        app=app_config,
        sources=raw.sources,
        missing_settings=missing_settings,
        settings_values=settings_values,
        config_file_found=raw.config_file_found,
        file_toml=raw.toml,
        dotenv_names=raw.dotenv_names,
    )


def _stored_settings(stored: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The grade-D rows that pass their rule, and the keys without one that does."""
    values: dict[str, Any] = {}
    missing: list[str] = []
    for key in GRADE_D_KEYS:
        if key not in stored:
            if key in REQUIRED_KEYS:
                missing.append(key)
            continue
        try:
            values[key] = validate_setting(key, stored[key])
        except ValueError as e:
            logger.warning("Ignoring D1 setting %s: %s", key, e)
            missing.append(key)
    return values, sorted(missing)


def _with_settings(raw_toml: dict, values: dict[str, Any], missing: list[str]) -> dict[str, Any]:
    """The file with its grade-D keys replaced by `values`, key by key.

    Key level, not table level: `[llm_provider] api_key` and `[vote_similarity]
    threshold` share a table with grade-D keys and still come from the file. A
    table missing any required grade-D key is None, file keys included: it cannot
    be used. An absent optional key is left out of `values` and the table's field
    default fills it.
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
    for table in _SETTINGS_TABLES:
        if any(key.split(".", 1)[0] == table for key in missing):
            merged[table] = None
        else:
            merged.setdefault(table, {})
    return merged
