"""Composition root: wire concrete adapters into the dependency container."""

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from cyris.adapters.anthropic_client import AnthropicClient
from cyris.adapters.fetch.newsletter_source import NewsletterArchiveSource
from cyris.adapters.gemini_client import GeminiClient
from cyris.adapters.notify import send_discord
from cyris.adapters.output.digest import DigestWriter
from cyris.adapters.output.usage_log import append_usage
from cyris.adapters.store import ArticleStore
from cyris.config import Config, LLMProviderConfig
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
    publish: Callable[[str], bool] | None
    sync_promotions: Callable[[], int] | None
    log_usage: Callable[..., None]
    send_discord: Callable[..., Any] = send_discord
    on_progress: Callable[[str], None] = field(default=lambda _msg: None)
    embedder: Any | None = None  # GeminiEmbedder when vote_similarity.enabled


def build_deps(cfg: Config, on_progress: Callable[[str], None] | None = None) -> Deps:
    store = ArticleStore(cfg.app.agent_vault.path)

    newsletter_source = NewsletterArchiveSource(cfg.app.agent_vault.path / "daily" / "newsletters")

    fetch_sources: list[FetchSource] = [newsletter_source]
    if cfg.app.newsletter.worker_url and cfg.app.newsletter.token:
        from cyris.adapters.fetch.newsletter_worker_source import CloudflareNewsletterSource

        fetch_sources.append(
            CloudflareNewsletterSource(cfg.app.newsletter.worker_url, cfg.app.newsletter.token)
        )

    # RSS comes from the Worker's hourly D1 buffer. Without it, direct polling is
    # the fallback — correct only for feeds whose snapshot outlives the window
    # (measured: a digest-time poll missed 141 of 317 articles). See
    # docs/cloud-migration.md#why-a-buffer-and-not-direct-polling.
    if cfg.app.rss.worker_url and cfg.app.rss.token:
        from cyris.adapters.fetch.rss_worker_source import CloudflareRssSource

        fetch_sources.append(CloudflareRssSource(cfg.app.rss.worker_url, cfg.app.rss.token))
    else:
        from cyris.adapters.fetch.rss_source import RssSource

        fetch_sources.append(RssSource())

    # Reuses the digest's own Gemini key: the Cloudflare token carries only
    # `account (read)` and Workers AI refuses it, so bge-m3 waits for the move.
    embedder = None
    if cfg.app.vote_similarity.enabled:
        from cyris.adapters.embedding import GeminiEmbedder

        embedder = GeminiEmbedder(
            api_key=os.environ.get("GEMINI_API_KEY", ""),
            cache_path=cfg.app.agent_vault.path / "embeddings.json",
            model=cfg.app.vote_similarity.model,
        )

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

    return Deps(
        cfg=cfg,
        store=store,
        llm=build_llm(cfg.app.llm_provider),
        fetch_sources=fetch_sources,
        writer=DigestWriter(cfg.app.obsidian.user_vault_path, cfg.app.obsidian.digest_folder),
        html_writer=html_writer,
        publish=publish,
        sync_promotions=sync,
        log_usage=partial(append_usage, log_path=cfg.app.agent_vault.path / "usage.jsonl"),
        on_progress=on_progress or (lambda _msg: None),
        embedder=embedder,
    )
