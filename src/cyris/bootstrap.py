"""Composition root: wire concrete adapters into the dependency container."""

import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache, partial
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

from cyris.adapters import process_usage
from cyris.adapters.ai_gateway_client import AIGatewayClient
from cyris.adapters.anthropic_client import AnthropicClient
from cyris.adapters.gemini_client import GeminiClient
from cyris.adapters.hosted_votes import sync_hosted_votes
from cyris.adapters.notify import send_discord, send_discord_alert
from cyris.adapters.openai_client import OpenAIClient
from cyris.adapters.output.usage_log import append_usage_d1
from cyris.adapters.promotions import SyncResult, sync_promotions
from cyris.adapters.workers_ai_client import WorkersAIClient
from cyris.config import Config, LLMProviderConfig
from cyris.domain.models import Article
from cyris.service_layer.alerts import RunAlert
from cyris.service_layer.ports import (
    ArticleRepository,
    AskClef,
    FetchSource,
    LLMClient,
    TopicRepository,
)


@cache
def _provider_defaults() -> dict:
    """Per-provider model defaults; see docs/architecture.md §5."""
    raw = (files("cyris") / "provider_defaults.json").read_text(encoding="utf-8")
    return {k: v for k, v in json.loads(raw).items() if not k.startswith("_")}


@cache
def _reasoning_data() -> dict:
    raw = (files("cyris") / "reasoning_effort_defaults.json").read_text(encoding="utf-8")
    return json.loads(raw)


def _reasoning_defaults() -> dict[str, str | None]:
    """Per-provider reasoning level sent when the setting is "default"."""
    return _reasoning_data()["defaults"]


def _reasoning_effort(cfg: LLMProviderConfig) -> str | None:
    if cfg.reasoning_effort != "default":
        return cfg.reasoning_effort
    return _reasoning_defaults()[cfg.provider]


def _gemini_effort(cfg: LLMProviderConfig, model: str) -> dict[str, str | int | None]:
    """The client keywords that carry the effort: a budget on Gemini 2.5, else a level."""
    effort = _reasoning_effort(cfg)
    spec = _reasoning_data()["gemini_thinking_budget"]
    if effort is not None and model.removeprefix("google/").startswith(
        tuple(spec["model_prefixes"])
    ):
        return {"thinking_budget": spec["budgets"][effort]}
    return {"reasoning_effort": effort}


def default_model(provider: str) -> str:
    """The model a provider runs with when the config names no model."""
    return _provider_defaults()["llm_models"][provider]


def default_models() -> dict[str, str]:
    """Every provider this build can wire, and what it defaults to."""
    return dict(_provider_defaults()["llm_models"])


def embedding_defaults(provider: str) -> dict:
    """The embedding model one provider defaults to."""
    return _provider_defaults()["embedding"][provider]


def build_llm(cfg: LLMProviderConfig) -> LLMClient | None:
    """Build the LLM client, or None when no provider/key is configured.

    None puts the pipeline in excerpt-only mode: LLM steps are skipped and the
    digest falls back to plain excerpts. Provider "none" chooses that mode, so it
    builds nothing even when a key is present.
    """
    if cfg.provider == "none":
        return None
    if not cfg.provider or not cfg.api_key:
        return None
    model = cfg.model or default_model(cfg.provider)
    if cfg.provider == "gemini":
        return GeminiClient(cfg.api_key, model, **_gemini_effort(cfg, model))
    if cfg.provider == "openai":
        return OpenAIClient(cfg.api_key, model, reasoning_effort=_reasoning_effort(cfg))
    if cfg.provider == "workers_ai":
        if not cfg.account_id:
            return None  # doctor names the missing CLOUDFLARE_ACCOUNT_ID
        return WorkersAIClient(cfg.api_key, cfg.account_id, model)
    if cfg.provider == "ai_gateway":
        if not cfg.account_id:
            return None  # doctor names the missing CLOUDFLARE_ACCOUNT_ID
        return AIGatewayClient(cfg.api_key, cfg.account_id, model, **_gemini_effort(cfg, model))
    return AnthropicClient(cfg.api_key, model, reasoning_effort=_reasoning_effort(cfg))


# The key each embedding provider reads, then anything else its REST path needs.
# workers_ai's is deliberately not CLOUDFLARE_API_TOKEN. That one carries D1 and
# Pages — measured 2026-08-30: D1 200, Pages 200, upload-token 200, Workers AI 401.
# Inference is a separate token because it is a separate permission.
EMBEDDING_ENV: dict[str, tuple[str, ...]] = {
    "workers_ai": ("CLOUDFLARE_EMBEDDING_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"),
    "gemini": ("GEMINI_API_KEY",),
}


def embedding_model(provider: str, model: str) -> str:
    """The model `provider` embeds with; "" is its default model."""
    return model or embedding_defaults(provider)["model"]


def make_embedder(provider: str, model: str) -> Any:
    """The embedder for `provider`, keyed from the environment; "" is its default model."""
    model = embedding_model(provider, model)
    env = [os.environ.get(name, "") for name in EMBEDDING_ENV[provider]]
    if provider == "gemini":
        from cyris.adapters.embedding import GeminiEmbedder

        return GeminiEmbedder(api_key=env[0], model=model)

    from cyris.adapters.embedding import WorkersAIEmbedder

    return WorkersAIEmbedder(api_token=env[0], account_id=env[1], model=model)


def build_embedder(cfg: Config) -> Any | None:
    """The run's embedder, or None when neither vote similarity nor a tracked topic needs one.

    Both read `[vote_similarity] provider` and `model`: the keys predate a second
    consumer, and renaming them would break every fork's settings.

    Neither adapter caches: a full run is ~600 texts, which bge-m3 prices at
    roughly 20 of a 10,000/day neuron allowance. The 338 MB cache that used to
    sit under this was optimising a cost that no longer exists.
    """
    vote = cfg.app.vote_similarity
    if not (vote.enabled or cfg.tracked_topics):
        return None
    return make_embedder(vote.provider, vote.model)


def build_worker_domains() -> Callable[[], list[str]] | None:
    """Ask Cloudflare for the app Worker's custom domains, or None without the three inputs.

    `CYRIS_APP_WORKER_NAME` comes from `[vars]` in wrangler.toml, so only the
    container a Worker starts has it.
    """
    from cyris.adapters.cloudflare import list_worker_domains

    inputs = [
        os.environ.get(name, "")
        for name in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN", "CYRIS_APP_WORKER_NAME")
    ]
    return partial(list_worker_domains, *inputs) if all(inputs) else None


def _email_api_credentials() -> tuple[str, str] | None:
    """The account and token Email Sending actually accepts, or None if either is missing.

    `CLOUDFLARE_API_TOKEN` is the account token. The Workers AI and embedding
    tokens are different permissions and cannot send mail.
    """
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if not (account_id and token):
        return None
    return account_id, token


def build_send_email(language: str = "") -> Callable[..., Any] | None:
    """The digest mail bound to this account's Email Sending API, or None without it.

    `language` is the current `digest.output_language`, which words an issue stored with none.
    """
    from cyris.adapters.mail import send_digest_mail

    credentials = _email_api_credentials()
    if credentials is None:
        return None
    account_id, token = credentials
    return partial(send_digest_mail, account_id=account_id, token=token, language=language)


def build_send_email_alert(**wording: Any) -> Callable[..., Any] | None:
    """The failure-alert mail bound to the same account, or None without it."""
    from cyris.adapters.mail import send_alert_mail

    credentials = _email_api_credentials()
    if credentials is None:
        return None
    account_id, token = credentials
    return partial(send_alert_mail, account_id=account_id, token=token, **wording)


def build_alert_senders(cfg: Config) -> tuple[Callable[..., Any], Callable[..., Any] | None]:
    """The failure alert's Discord and mail senders, to the reader's own channels.

    A hosted tenant's reader is sent each alert with its error withheld; the raw error is the
    operator's (`build_operator_alert`, ADR-0035). `build_deps` and the CLI's could-not-start
    alert both take them from here, so no alert path of a hosted run shows its reader the error.
    """
    from cyris.adapters import notify

    if cfg.app.store.is_hosted:
        from cyris.adapters.run_alert import reader_alert_message

        return (
            partial(notify.send_discord_alert, wording=reader_alert_message),
            build_send_email_alert(wording=reader_alert_message),
        )
    return notify.send_discord_alert, build_send_email_alert()


def build_operator_alert(cfg: Config) -> Callable[[RunAlert], Awaitable[None]] | None:
    """The hosted operator's channel, bound to this run's tenant; None outside hosted mode."""
    if not cfg.app.store.is_hosted:
        return None
    from cyris.adapters.hosted import send_operator_alert

    return partial(send_operator_alert, cfg.app.store.tenant_id)


def clef_credentials() -> tuple[str, str] | None:
    """The account and the Workers AI token, else the embedding one; None without either.

    /settings reads it too, to disable Clef where a run could not call it.
    """
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    token = os.environ.get("CLOUDFLARE_AI_TOKEN", "") or os.environ.get(
        "CLOUDFLARE_EMBEDDING_API_TOKEN", ""
    )
    if not (account_id and token):
        return None
    return account_id, token


def build_ask_clef() -> AskClef | None:
    """Clef bound to the Workers AI token, else the embedding one; None without either."""
    from cyris.adapters.clef import ClefClient

    credentials = clef_credentials()
    if credentials is None:
        return None
    account_id, token = credentials
    return ClefClient(token, account_id).ask


def build_d1_client(cfg: Config) -> Any:
    """The D1 connection: a hosted tenant's Durable Object, Cloudflare's D1, or the local file."""
    if cfg.app.store.operator:
        # The operator, outside any run, named this tenant (`cyris --tenant`): the hosted
        # Worker's Access-checked route on the hosted domain answers for its database (ADR-0036).
        from cyris.adapters.store.tenant_gateway import OperatorD1Client
        from cyris.config import OPERATOR_TOKEN_ENV_VARS

        if missing := cfg.missing_store_keys():
            raise ValueError(
                f"Missing required environment variables: {', '.join(missing)}, the Access "
                f"service token that reaches tenant {cfg.app.store.tenant_id} as the operator"
            )
        client_id, client_secret = (os.environ[name] for name in OPERATOR_TOKEN_ENV_VARS)
        return OperatorD1Client(
            cfg.app.store.tenant_id, cfg.app.store.hosted_domain, client_id, client_secret
        )

    if cfg.app.store.is_hosted:
        # The hosted Worker started this container with the tenant's id and intercepts the
        # gateway's host for it; the container holds no D1 credentials (ADR-0028).
        from cyris.adapters.store.tenant_gateway import GatewayD1Client

        return GatewayD1Client(cfg.app.store.tenant_id)

    if not cfg.app.store.is_cloudflare:
        from cyris.adapters.store.local_d1 import LocalD1Client

        return LocalD1Client(cfg.app.store.sqlite_path)

    # Named here, not at the first request: a client built on blank credentials
    # retries four times and dies on `Bearer ` with nothing a reader can act on,
    # and it does so a line before `validate_required_keys` would have run.
    missing = cfg.missing_store_keys()
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

    from cyris.adapters.store.d1 import D1Client

    return D1Client(
        account_id=cfg.app.store.account_id,
        database_id=cfg.app.store.database_id,
        api_token=cfg.app.store.api_token,
    )


def build_settings(cfg: Config, d1: Any | None = None) -> Any:
    """The settings store, on `d1` or on a client of its own."""
    from cyris.adapters.store.settings import D1Settings

    return D1Settings(d1 or build_d1_client(cfg))


def load_effective_config(
    config_path: Path,
    sources_path: Path,
    *,
    local_file: Literal["open", "if_created", "never"] = "open",
) -> Config:
    """The config a run actually uses, with grade-D settings from one home.

    They come from the store's `settings` table alone, whatever the file says.
    Every entrypoint goes through here, because a host run and a container run
    resolving settings differently is the failure this exists to prevent — so a D1
    read error propagates.

    `local_file` says what happens to a `sqlite` install's file. The server, the
    wizard and a run open it, which creates it on first use. A command that only
    reports (`if_created`) leaves a file that is not there uncreated, and one that
    writes elsewhere (`never`) leaves it alone: either gets the store as empty.
    """
    from cyris.adapters.store.areas import D1AreaStore
    from cyris.adapters.store.d1 import apply_schema
    from cyris.adapters.store.source_store import D1SourceStore
    from cyris.adapters.store.tracked_topics import D1TrackedTopicStore
    from cyris.config import read_config_files, resolve_config

    raw = read_config_files(config_path, sources_path)
    # Grade-D keys from nowhere yet: this pass only has to know the store, and
    # must not fail on file values a D1 deployment is about to ignore.
    cfg = resolve_config(raw, d1_settings={})
    if cfg.app.store.backend == "sqlite" and (
        local_file == "never" or (local_file == "if_created" and cfg.app.store.sqlite_not_created)
    ):
        cfg.sources = {}
        return cfg

    d1 = build_d1_client(cfg)
    settings = build_settings(cfg, d1)
    # First boot on a clean account: nothing else creates the tables, and both
    # reads below propagate by design, so an empty D1 would abort the CLI before
    # any check could name the cause. Idempotent, one POST.
    apply_schema(d1)
    if cfg.app.store.is_hosted:
        # The tenant's own pages table, which no self-hosted store has (ADR-0029). One query.
        from cyris.adapters.store.tenant_gateway import apply_hosted_schema

        apply_hosted_schema(d1)
    stored = settings.all()
    if cfg.app.store.is_hosted:
        # What a tenant does not decide, whatever its database holds (ADR-0040).
        from cyris.adapters.hosted import pin_tenant_settings

        stored = pin_tenant_settings(stored)
    cfg = resolve_config(raw, d1_settings=stored)
    # The table alone, empty included: sources.yaml is not a run's list.
    cfg.sources = D1SourceStore(d1).list_sources()
    cfg.tracked_topics = D1TrackedTopicStore(d1).list_topics()
    cfg.areas = D1AreaStore(d1).list_areas()
    return cfg


def build_store(cfg: Config, d1: Any | None = None) -> ArticleRepository:
    """The article store, on `d1` or on a client of its own."""
    from cyris.adapters.store.d1_store import D1ArticleStore

    return D1ArticleStore(d1 or build_d1_client(cfg))


def build_topic_store(cfg: Config, d1: Any | None = None) -> TopicRepository:
    """Where topics and topic votes live, on `d1` or on a client of its own."""
    from cyris.adapters.store.topics import D1TopicStore

    return D1TopicStore(d1 or build_d1_client(cfg))


@dataclass(frozen=True)
class Deps:
    """Concrete dependencies for the use cases; built once per invocation."""

    cfg: Config
    store: ArticleRepository
    llm: LLMClient | None  # None ⇒ degraded (excerpt-only) mode
    fetch_sources: list[FetchSource]
    html_writer: Any | None  # HtmlDigestWriter when html_output.enabled
    sync_promotions: Callable[[], SyncResult] | None
    log_usage: Callable[..., None]
    tag_store: Any | None = None
    story_store: Any | None = None
    # None only for a test that builds Deps by hand; build_deps always wires one.
    topic_store: TopicRepository | None = None
    # Set instead of `publish` when the site is published from the D1 manifest:
    # takes {path: bytes} for this run's pages. Nothing touches the filesystem.
    publish_site: Callable[[dict[str, bytes], str], bool] | None = None
    # Takes (path, expected bytes) and says whether the live page serves them; bound to
    # the Pages project beside `publish_site`. None without the site publisher.
    page_serves: Callable[[str, bytes], bool] | None = None
    site_filenames: Callable[[], list[str]] = field(default_factory=lambda: list)
    send_discord: Callable[..., Any] = send_discord
    on_progress: Callable[[str], None] = field(default=lambda _msg: None)
    # None ⇒ vote similarity is switched off and no topic is tracked.
    embedder: Any | None = None
    # The model `embedder` embeds with, resolved: a tracked topic set for any
    # other model is skipped.
    embedding_model: str = ""
    # The reader's areas: where a run's and an articles score's suggestions are counted.
    area_store: Any | None = None
    # Takes the final `run_summary` dict.
    record_run: Callable[[dict], None] | None = None
    # Takes (run_at, period, verdicts) after a vote-similarity pass that embedded.
    record_similarity: Callable[..., None] | None = None
    # The holds a run honours and records: D1Suppressions.
    suppressions: Any | None = None
    # `{(date, period): articles_included}` for the archive's history rows.
    archive_counts: Callable[[], dict[tuple[str, str], int]] = field(default_factory=lambda: dict)
    # `{(date, period): started_at}`, the start each archived issue is named by.
    archive_starts: Callable[[], dict[tuple[str, str], datetime]] | None = None
    # Keeps each issue's final content in D1.
    digest_store: Any | None = None
    # Each publishing run's input URLs, the record a rerun rebuilds from.
    digest_input_store: Any | None = None
    # The app Worker's custom domains, for the digest link when none is configured;
    # None where the Worker is unknown, as in a compose install.
    worker_domains: Callable[[], list[str]] | None = None
    # Beside `send_discord`, not behind a sink list: the email channel is a
    # prototype, and the multi-channel shape waits until it is done (2026-09-27).
    # None without a Cloudflare account and token to send with.
    send_email: Callable[..., Any] | None = None
    # Failure alerts are their own callables, not a sink list: each channel is
    # reached by name, and a missing mail sender is None rather than an entry.
    send_email_alert: Callable[..., Any] | None = None
    send_discord_alert: Callable[..., Any] = send_discord_alert
    # Takes the `RunAlert` every alert path also hands the reader's channels, and tells the
    # hosted operator, whatever the tenant's own `notify` says. None outside hosted mode.
    send_operator_alert: Callable[[RunAlert], Awaitable[None]] | None = None
    # None without a Workers AI (or embedding) token and an account id.
    ask_clef: AskClef | None = None
    # Takes the run's articles and `summarize_snippet_length`, and returns them with
    # each RSS teaser on its page's longer body, for this run only. None unless
    # `build_deps` was asked for it: `run` (a `--dry-run` too) and a `regenerate`
    # rerun fetch pages; a rerender and the comparison commands fetch none.
    lengthen_articles: Callable[..., Awaitable[list[Article]]] | None = None
    # Takes nothing and returns `cpu_seconds` and `peak_rss_bytes`, which the run
    # adds to its summary as it ends. None leaves the summary without them.
    process_usage: Callable[[], dict[str, float | int]] | None = None
    # A hosted run's AI spend (ADR-0038): charges what the run's metered LLM, embedder and
    # Clef spent to the tenant's month as the run ends, failed or not, and returns it for the
    # summary. None outside a hosted tenant's run, which changes nothing.
    record_spend: Callable[[], dict] | None = None


def build_promotion_sync(
    cfg: Config,
    store: ArticleRepository | None = None,
    topics: TopicRepository | None = None,
    d1: Any | None = None,
) -> Callable[[], SyncResult] | None:
    """The vote pull bound to `store`, or None when no vote Worker is configured.

    A hosted tenant's pull is its own database's `hosted_votes` queue, read through `d1`
    (ADR-0039); with no `d1`, as in `promote-sync`, it has none.

    Reads no runtime setting, so `promote-sync` can call it on an empty D1. With
    no `store` it builds one, and only once a Worker is configured: a SQLite store
    creates its file.
    """
    promote = cfg.app.promote
    if cfg.app.store.is_hosted:
        # The vote Worker's queue is one deployment's: a hosted tenant draining it would
        # acknowledge another reader's votes it cannot match (ADR-0028). Its votes are its own
        # table, which the hosted Worker fills from a signed-in reader's session (ADR-0039).
        if d1 is None or store is None:
            return None
        return partial(sync_hosted_votes, d1, store, topics)
    if not (promote.worker_url and promote.token):
        return None
    store = store if store is not None else build_store(cfg)
    topics = topics if topics is not None else build_topic_store(cfg)
    return partial(sync_promotions, promote.worker_url, promote.token, store, topics)


def build_rss_source(cfg: Config, d1: Any | None = None) -> FetchSource:
    """The run's RSS edge: an hourly buffer, else direct polling.

    Direct polling is the fallback — correct only for feeds whose snapshot
    outlives the window (measured: a digest-time poll missed 141 of 317
    articles). See docs/decisions/0001-rss-arrives-through-an-hourly-buffer.md.

    A hosted tenant reads its own buffer, which the hosted Worker polls into its
    Durable Object hourly, through `d1` or a gateway client of its own (ADR-0031);
    the rss Worker's buffer is another deployment's.
    """
    if cfg.app.store.is_hosted:
        from cyris.adapters.fetch.rss_worker_source import TenantRssSource

        return TenantRssSource(d1 or build_d1_client(cfg))

    if cfg.app.rss.worker_url and cfg.app.rss.token:
        from cyris.adapters.fetch.rss_worker_source import CloudflareRssSource

        return CloudflareRssSource(cfg.app.rss.worker_url, cfg.app.rss.token)

    from cyris.adapters.fetch.rss_source import RssSource

    return RssSource()


def build_onboarding_articles(cfg: Config) -> Any:
    """The onboarding wizard's card reader, over the very RSS edge a run reads.

    Built from `build_rss_source`, so a card's URL comes through the parser and
    the URL normalisation the first run's fetch uses. With the Worker it polls
    first, since the feeds were picked a moment ago and the cron may be an hour off.

    A hosted tenant's cards still poll the picked feeds directly, as they did before its
    buffer existed: per-tenant onboarding is a card of its own, and the hosted Worker
    serves no wizard yet.
    """
    from cyris.adapters.fetch.onboarding_cards import OnboardingArticles
    from cyris.adapters.fetch.rss_worker_source import CloudflareRssSource

    if cfg.app.store.is_hosted:
        from cyris.adapters.fetch.rss_source import RssSource

        return OnboardingArticles(RssSource(), poll=None)

    source = build_rss_source(cfg)
    poll = source.poll if isinstance(source, CloudflareRssSource) else None
    return OnboardingArticles(source, poll=poll)


def build_feed_checker() -> Any:
    """The onboarding wizard's check of a feed the reader pastes, over public addresses only.

    It reads no setting and no store: one fetch of the address, and one more
    when that is a home page linking its feed.
    """
    from cyris.adapters.fetch.feed_check import FeedChecker

    return FeedChecker()


def build_deps(
    cfg: Config,
    on_progress: Callable[[str], None] | None = None,
    dry_run: bool = False,
    fetch_pages: bool = False,
) -> Deps:
    """Wire the run's IO. `dry_run` decides it here, not deeper.

    `fetch_pages` wires `lengthen_articles`; only a digest run asks for it, so a
    command that merely builds the run's wiring sends no request to a publisher.

    A preview skips the writes inside `run_digest`, but two of the edges wired
    below consume rather than read: the newsletter queue deletes what it hands
    over, and a vote sync stamps human verdicts before draining its own queue.
    Neither is undone by not saving afterwards, so a preview must be handed
    sources that do not consume — which is a wiring decision, and this is where
    every other one lives.

    A hosted tenant's run is wired the same way for the same reason: it gets neither
    the newsletter Worker's queue nor the vote sync, which are another deployment's
    queues, a digest notification that sends nothing, and alerts that tell the operator
    the error and the reader only that it happened (`adapters/hosted.py`, ADR-0035).
    Its newsletters are its own queue in its Durable Object, taken with the same
    `ack=not dry_run` (ADR-0034).
    Its pages go to its own site in its Durable Object (`HostedPages`), never to
    Pages, whatever `html_output` and `promote` say, and its links point at the
    hosted Worker (ADR-0029).
    """
    from cyris.adapters.store.archive_meta import D1ArchiveMeta
    from cyris.adapters.store.areas import D1AreaStore
    from cyris.adapters.store.digest_inputs import D1DigestInputStore
    from cyris.adapters.store.digests import D1DigestStore
    from cyris.adapters.store.runs import D1RunLog
    from cyris.adapters.store.similarity_shadow import D1SimilarityShadow
    from cyris.adapters.store.stories import D1StoryStore
    from cyris.adapters.store.suppressions import D1Suppressions
    from cyris.adapters.store.tags import D1TagStore

    # Spend is logged beside the articles, in the same database.
    d1 = build_d1_client(cfg)
    store = build_store(cfg, d1)
    vote = cfg.app.vote_similarity
    # The current setting: the archive's language, and an issue's when it stored none.
    language = cfg.app.digest.output_language if cfg.app.digest else ""
    hosted = cfg.app.store.is_hosted

    fetch_sources: list[FetchSource] = []
    if hosted:
        # The tenant's own queue, which the hosted Worker's email() handler fills from the
        # tenant's random address (ADR-0034); a preview reads it without the ack's delete.
        from cyris.adapters.fetch.newsletter_worker_source import TenantNewsletterSource

        fetch_sources.append(TenantNewsletterSource(d1, ack=not dry_run))
    elif cfg.app.newsletter.worker_url and cfg.app.newsletter.token:
        from cyris.adapters.fetch.newsletter_worker_source import CloudflareNewsletterSource

        fetch_sources.append(
            CloudflareNewsletterSource(
                cfg.app.newsletter.worker_url,
                cfg.app.newsletter.token,
                ack=not dry_run,
            )
        )

    fetch_sources.append(build_rss_source(cfg, d1))

    # A tenant's run is held to its monthly AI allowance in the operator's registry, through
    # the hosted Worker (ADR-0038); not on the operator's machine, which starts no run
    # (ADR-0036). Asked here, before anything is wired: a month already spent wires no model.
    ledger = None
    ai_pause = None
    paused = None
    if hosted and not cfg.app.store.operator:
        from cyris.adapters.hosted_spend import OFF, SpendLedger
        from cyris.adapters.output.html_digest import AiPause

        ledger = SpendLedger(cfg.app.store.tenant_id)
        if paused := ledger.spent():
            # To the second, as `run_digest` stamps the issue's start.
            ai_pause = AiPause(since=datetime.now(UTC).replace(microsecond=0), off=paused == OFF)
            (on_progress or (lambda _msg: None))(
                f"This tenant's AI allowance is {'0' if paused == OFF else 'spent this month'}: "
                "this run calls no model and its digest lists plain excerpts."
            )

    html_writer = None
    publish_site = None
    page_serves = None
    site_filenames: Callable[[], list[str]] = list
    worker_domains = None if hosted else build_worker_domains()
    if hosted or cfg.app.html_output.enabled:
        from cyris.adapters.output.hosted_pages import tenant_settings_path
        from cyris.adapters.output.html_digest import SETTINGS_PATH, HtmlDigestWriter

        html_writer = HtmlDigestWriter(
            Path(cfg.app.html_output.output_dir),
            language=language,
            starts=D1ArchiveMeta(d1).issue_starts,
            ai_pause=ai_pause,
            # A tenant's pages are served under /t/<tenant>/, where `/settings` is an operator
            # route; its Settings link is the tenant's own page (ADR-0040).
            settings_path=(
                tenant_settings_path(cfg.app.store.tenant_id) if hosted else SETTINGS_PATH
            ),
        )
    if hosted:
        # The tenant's site is its own Durable Object, which the hosted Worker serves behind
        # sign-in. The digest link's "Worker domain" is that site's root on the hosted Worker,
        # so `run_digest` links https://<hosted domain>/t/<tenant>/<slug> (ADR-0029).
        from cyris.adapters.output.hosted_pages import HostedPages, tenant_site

        site = HostedPages(d1)
        publish_site = site.publish
        page_serves = site.serves
        site_filenames = site.filenames
        tenant_root = tenant_site(cfg.app.store.hosted_domain, cfg.app.store.tenant_id)
        worker_domains = lambda: [tenant_root]  # noqa: E731
    elif html_writer is not None and cfg.app.promote.publish_enabled:
        # The site's file list lives in D1, so the archive does not have
        # to live on this machine.
        from cyris.adapters.output.pages_manifest import D1PagesManifest
        from cyris.adapters.output.pages_receipt import D1PagesDeployReceipt
        from cyris.adapters.output.publish import page_serves as _serves
        from cyris.adapters.output.publish import publish_site as _site

        manifest = D1PagesManifest(d1)
        publish_site = partial(
            _site,
            manifest_store=manifest,
            pages_project=cfg.app.promote.pages_project,
            receipt_store=D1PagesDeployReceipt(d1),
        )
        page_serves = partial(_serves, cfg.app.promote.pages_project)
        site_filenames = lambda: [p.lstrip("/") for p in manifest.load()]  # noqa: E731

    topic_store = build_topic_store(cfg, d1)
    sync = None if dry_run else build_promotion_sync(cfg, store, topic_store, d1)

    lengthen_articles = None
    if fetch_pages:
        from cyris.adapters.fetch.article_page import lengthen_articles as _lengthen

        lengthen_articles = partial(_lengthen, sources=cfg.sources)

    send_discord_alert_to, send_email_alert = build_alert_senders(cfg)
    if hosted:
        from cyris.adapters.hosted import skip_digest_notification

        notify_discord: Callable[..., Any] = skip_digest_notification
        send_email: Callable[..., Any] | None = skip_digest_notification
    else:
        notify_discord = partial(send_discord, language=language)
        send_email = build_send_email(language)

    llm = build_llm(cfg.app.llm_provider)
    embedder = build_embedder(cfg)
    ask_clef = build_ask_clef()
    record_spend = None
    if ai_pause is not None:
        # The plain list, by the path a missing client takes; the page says why (`AiPause`).
        llm = embedder = ask_clef = None
    if ledger is not None:
        # Every model call, embedding and Clef question the tenant's run makes is metered as
        # it is made and charged to the tenant's month as the run ends (ADR-0038).
        from cyris.adapters.hosted_spend import (
            MeteredClef,
            MeteredEmbedder,
            MeteredLLM,
            SpendMeter,
        )

        meter = SpendMeter(llm.model if llm is not None else "")
        llm = MeteredLLM(llm, meter) if llm is not None else None
        if embedder is not None:
            embedder = MeteredEmbedder(embedder, meter, cfg.app.vote_similarity.provider)
        ask_clef = MeteredClef(ask_clef, meter) if ask_clef is not None else None
        record_spend = partial(ledger.report, meter, paused=paused)

    return Deps(
        cfg=cfg,
        store=store,
        llm=llm,
        fetch_sources=fetch_sources,
        html_writer=html_writer,
        publish_site=publish_site,
        page_serves=page_serves,
        site_filenames=site_filenames,
        sync_promotions=sync,
        log_usage=partial(append_usage_d1, client=d1),
        tag_store=D1TagStore(d1),
        story_store=D1StoryStore(d1),
        topic_store=topic_store,
        on_progress=on_progress or (lambda _msg: None),
        embedder=embedder,
        embedding_model=embedding_model(
            cfg.app.vote_similarity.provider, cfg.app.vote_similarity.model
        ),
        area_store=D1AreaStore(d1),
        record_run=D1RunLog(d1, os.environ.get("CYRIS_GIT_SHA", "")).record,
        record_similarity=D1SimilarityShadow(d1, embedding_model(vote.provider, vote.model)).record,
        suppressions=D1Suppressions(d1, embedding_model(vote.provider, vote.model)),
        archive_counts=D1ArchiveMeta(d1).article_counts,
        archive_starts=D1ArchiveMeta(d1).issue_starts,
        digest_store=D1DigestStore(d1),
        digest_input_store=D1DigestInputStore(d1),
        worker_domains=worker_domains,
        send_discord=notify_discord,
        send_email=send_email,
        send_email_alert=send_email_alert,
        send_discord_alert=send_discord_alert_to,
        send_operator_alert=build_operator_alert(cfg),
        ask_clef=ask_clef,
        lengthen_articles=lengthen_articles,
        process_usage=process_usage.read_process_usage if process_usage.available() else None,
        record_spend=record_spend,
    )
