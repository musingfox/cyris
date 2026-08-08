"""Composition root: wire concrete adapters into the dependency container."""

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from cyris.adapters.anthropic_client import AnthropicClient
from cyris.adapters.cookies import load_browser_cookies
from cyris.adapters.fetch.miniflux import MinifluxClient
from cyris.adapters.fetch.miniflux_source import MinifluxSource
from cyris.adapters.fetch.newsletter_source import NewsletterArchiveSource
from cyris.adapters.gemini_client import GeminiClient
from cyris.adapters.notify import send_discord
from cyris.adapters.output.digest import DigestWriter
from cyris.adapters.output.usage_log import append_usage
from cyris.adapters.store import ArticleStore
from cyris.adapters.store.event_store import EventStore
from cyris.config import Config, LLMProviderConfig, VaultConfigSource
from cyris.service_layer.ports import FetchSource, LLMClient

_DEFAULT_MODELS = {"anthropic": "claude-sonnet-4-6", "gemini": "gemini-2.5-flash"}


def build_llm(cfg: LLMProviderConfig) -> LLMClient | None:
    """Build the LLM client, or None when no provider/key is configured.

    None puts the pipeline in degraded mode: LLM steps are skipped and the
    digest falls back to plain excerpts.
    """
    if not cfg.provider or not cfg.api_key:
        return None
    model = cfg.model or _DEFAULT_MODELS[cfg.provider]
    if cfg.provider == "gemini":
        return GeminiClient(cfg.api_key, model)
    return AnthropicClient(cfg.api_key, model)


@dataclass(frozen=True)
class Deps:
    """Concrete dependencies for the use cases; built once per invocation."""

    cfg: Config
    store: ArticleStore
    llm: LLMClient | None  # None ⇒ degraded (excerpt-only) mode
    fetch_sources: list[FetchSource]
    writer: DigestWriter
    html_writer: Any | None  # HtmlDigestWriter when html_output.enabled
    publish: Callable[[], bool] | None
    sync_promotions: Callable[[], int] | None
    load_cookies: Callable[[], dict[str, str] | None]
    log_usage: Callable[..., None]
    send_discord: Callable[..., Any] = send_discord
    on_progress: Callable[[str], None] = field(default=lambda _msg: None)
    tracking: VaultConfigSource | None = None
    event_store: EventStore | None = None


def build_deps(cfg: Config, on_progress: Callable[[str], None] | None = None) -> Deps:
    store = ArticleStore(cfg.app.agent_vault.path)

    miniflux_source = MinifluxSource(MinifluxClient(cfg.app.miniflux.url, cfg.app.miniflux.api_key))
    newsletter_source = NewsletterArchiveSource(cfg.app.agent_vault.path / "daily" / "newsletters")

    fetch_sources: list[FetchSource] = [miniflux_source, newsletter_source]
    if cfg.app.newsletter.worker_url and cfg.app.newsletter.token:
        from cyris.adapters.fetch.newsletter_worker_source import CloudflareNewsletterSource

        fetch_sources.append(
            CloudflareNewsletterSource(cfg.app.newsletter.worker_url, cfg.app.newsletter.token)
        )

    # Opt-in while Miniflux stays up: both run, and fetch_all_articles dedups by URL.
    if cfg.app.rss.worker_url and cfg.app.rss.token:
        from cyris.adapters.fetch.rss_worker_source import CloudflareRssSource

        fetch_sources.append(CloudflareRssSource(cfg.app.rss.worker_url, cfg.app.rss.token))

    html_writer = None
    publish = None
    if cfg.app.html_output.enabled:
        from cyris.adapters.output.html_digest import HtmlDigestWriter
        from cyris.adapters.output.publish import publish_html_digest

        html_writer = HtmlDigestWriter(
            Path(cfg.app.html_output.output_dir),
            promote_worker_url=cfg.app.promote.worker_url,
            promote_token=cfg.app.promote.token,
        )
        if cfg.app.promote.publish_enabled:
            publish = partial(
                publish_html_digest,
                Path(cfg.app.html_output.output_dir),
                cfg.app.promote.pages_project,
            )

    sync = None
    if cfg.app.promote.worker_url and cfg.app.promote.token:
        from cyris.adapters.promotions import sync_promotions

        sync = partial(
            sync_promotions,
            cfg.app.promote.worker_url,
            cfg.app.promote.token,
            store,
        )

    def load_cookies() -> dict[str, str] | None:
        if not cfg.app.paywall.use_browser_cookies:
            return None
        return load_browser_cookies(cfg.app.paywall.browser, cfg.app.paywall.cookie_domains)

    tracking = VaultConfigSource(cfg.app.agent_vault.path / cfg.app.agent_vault.tracking_file)
    event_store = EventStore(cfg.app.agent_vault.path / "events")

    return Deps(
        cfg=cfg,
        store=store,
        llm=build_llm(cfg.app.llm_provider),
        fetch_sources=fetch_sources,
        writer=DigestWriter(cfg.app.obsidian.user_vault_path, cfg.app.obsidian.digest_folder),
        html_writer=html_writer,
        publish=publish,
        sync_promotions=sync,
        load_cookies=load_cookies,
        log_usage=partial(append_usage, log_path=cfg.app.agent_vault.path / "usage.jsonl"),
        on_progress=on_progress or (lambda _msg: None),
        tracking=tracking,
        event_store=event_store,
    )
