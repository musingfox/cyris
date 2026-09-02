"""Composition root: wire concrete adapters into the dependency container."""

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from cyris.adapters.anthropic_client import AnthropicClient
from cyris.adapters.gemini_client import GeminiClient
from cyris.adapters.notify import send_discord
from cyris.adapters.openai_client import OpenAIClient
from cyris.adapters.output.usage_log import append_usage, append_usage_d1
from cyris.adapters.store import ArticleStore
from cyris.adapters.workers_ai_client import WorkersAIClient
from cyris.config import Config, LLMProviderConfig
from cyris.service_layer.ports import ArticleRepository, FetchSource, LLMClient

_DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "gemini": "gemini-2.5-flash",
    "openai": "gpt-5.6-luna",
    # gpt-oss-120b rather than llama-3.3-70b: the filter tier sends its articles as
    # one un-batched prompt, and llama's 24k context leaves no headroom for a busy
    # window. gpt-oss has 128k, and its output rate is ~3x cheaper besides
    # (68,182 vs 204,805 neurons per M output tokens).
    "workers_ai": "@cf/openai/gpt-oss-120b",
}


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
    if cfg.provider == "openai":
        return OpenAIClient(cfg.api_key, model)
    if cfg.provider == "workers_ai":
        if not cfg.account_id:
            return None  # doctor names the missing CLOUDFLARE_ACCOUNT_ID
        return WorkersAIClient(cfg.api_key, cfg.account_id, model)
    return AnthropicClient(cfg.api_key, model)


# Each embedding model has its own calibrated cutoff and its own default model id.
# See docs/vote-signal-measurement.md — the scales differ, the discrimination does not.
_EMBEDDING_DEFAULTS = {
    "workers_ai": {"model": "@cf/baai/bge-m3", "threshold": 0.53},
    "gemini": {"model": "gemini-embedding-001", "threshold": 0.68},
}


def build_embedder(cfg: Config) -> Any | None:
    """The embedder for vote similarity, or None when it is switched off.

    Neither adapter caches: a full run is ~600 texts, which bge-m3 prices at
    roughly 20 of a 10,000/day neuron allowance. The 338 MB cache that used to
    sit under this was optimising a cost that no longer exists.
    """
    vote = cfg.app.vote_similarity
    if not vote.enabled:
        return None
    defaults = _EMBEDDING_DEFAULTS[vote.provider]
    model = vote.model or defaults["model"]

    if vote.provider == "gemini":
        from cyris.adapters.embedding import GeminiEmbedder

        return GeminiEmbedder(api_key=os.environ.get("GEMINI_API_KEY", ""), model=model)

    from cyris.adapters.embedding import WorkersAIEmbedder

    # Deliberately not CLOUDFLARE_API_TOKEN. That one carries D1 and Pages —
    # measured 2026-08-30: D1 200, Pages 200, upload-token 200, Workers AI 401.
    # Inference is a separate token because it is a separate permission.
    return WorkersAIEmbedder(
        api_token=os.environ.get("CLOUDFLARE_EMBEDDING_API_TOKEN", ""),
        account_id=os.environ.get("CLOUDFLARE_ACCOUNT_ID", ""),
        model=model,
    )


def embedding_threshold(cfg: Config) -> float:
    """The configured cutoff, or the provider's own calibration."""
    vote = cfg.app.vote_similarity
    if vote.threshold is not None:
        return vote.threshold
    return _EMBEDDING_DEFAULTS[vote.provider]["threshold"]


def build_d1_client(cfg: Config) -> Any | None:
    """The D1 connection, or None when `[store] backend` is still json."""
    if not cfg.app.store.is_d1:
        return None

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


def build_settings(cfg: Config, d1: Any | None = None) -> Any | None:
    """The D1 settings store, or None when there is no D1 to read it from."""
    d1 = d1 if d1 is not None else build_d1_client(cfg)
    if d1 is None:
        return None

    from cyris.adapters.store.settings import D1Settings

    return D1Settings(d1)


def load_effective_config(config_path: Path, sources_path: Path) -> Config:
    """The config a run actually uses: the file, then D1's grade-D overrides.

    The single seam where the read order from `adapters/store/settings.py` is
    applied. Every entrypoint goes through here, because a host run and a
    container run resolving settings differently is the failure this exists to
    prevent — so a D1 read error propagates rather than quietly using the file.
    """
    from cyris.adapters.store.settings import apply_to
    from cyris.config import load_config

    cfg = load_config(config_path, sources_path)
    settings = build_settings(cfg)
    if settings is not None:
        cfg.settings_from_d1 = apply_to(cfg, settings.all())
    return cfg


def build_store(cfg: Config, d1: Any | None = None) -> ArticleRepository:
    """The article store, JSON files or D1 depending on `[store] backend`."""
    d1 = d1 or build_d1_client(cfg)
    if d1 is None:
        return ArticleStore(cfg.app.agent_vault.path)

    from cyris.adapters.store.d1_store import D1ArticleStore

    return D1ArticleStore(d1)


@dataclass(frozen=True)
class Deps:
    """Concrete dependencies for the use cases; built once per invocation."""

    cfg: Config
    store: ArticleRepository
    llm: LLMClient | None  # None ⇒ degraded (excerpt-only) mode
    fetch_sources: list[FetchSource]
    html_writer: Any | None  # HtmlDigestWriter when html_output.enabled
    publish: Callable[[str], bool] | None
    sync_promotions: Callable[[], int] | None
    log_usage: Callable[..., None]
    tag_store: Any | None = None
    story_store: Any | None = None
    # Set instead of `publish` when the site is published from the D1 manifest:
    # takes {path: bytes} for this run's pages. Nothing touches the filesystem.
    publish_site: Callable[[dict[str, bytes], str], bool] | None = None
    site_filenames: Callable[[], list[str]] = field(default_factory=lambda: list)
    send_discord: Callable[..., Any] = send_discord
    on_progress: Callable[[str], None] = field(default=lambda _msg: None)
    embedder: Any | None = None  # None ⇒ vote similarity is switched off
    embedding_threshold: float = 0.68  # the embedder's own calibration, not a preference


def build_deps(cfg: Config, on_progress: Callable[[str], None] | None = None) -> Deps:
    # Spend is logged beside the articles: the same D1 when D1 is on, else usage.jsonl.
    d1 = build_d1_client(cfg)
    store = build_store(cfg, d1)
    log_usage = (
        partial(append_usage_d1, client=d1)
        if d1
        else partial(append_usage, log_path=cfg.app.agent_vault.path / "usage.jsonl")
    )
    tag_store = None
    story_store = None
    if d1 is not None:
        from cyris.adapters.store.stories import D1StoryStore
        from cyris.adapters.store.tags import D1TagStore

        tag_store = D1TagStore(d1)
        story_store = D1StoryStore(d1)

    fetch_sources: list[FetchSource] = []
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

    html_writer = None
    publish = None
    publish_site = None
    site_filenames: Callable[[], list[str]] = list
    if cfg.app.html_output.enabled:
        from cyris.adapters.output.html_digest import HtmlDigestWriter

        html_writer = HtmlDigestWriter(
            Path(cfg.app.html_output.output_dir),
            promote_worker_url=cfg.app.promote.worker_url,
            promote_token=cfg.app.promote.token,
        )
        if cfg.app.promote.publish_enabled:
            from cyris.adapters.output.publish import publish_html_digest
            from cyris.adapters.output.publish import publish_site as _site

            if d1 is not None:
                # The site's file list lives in D1, so the archive does not have
                # to live on this machine. Local files are the no-D1 fallback,
                # same shape as `sources.yaml` behind the `sources` table.
                from cyris.adapters.output.pages_manifest import D1PagesManifest

                manifest = D1PagesManifest(d1)
                publish_site = partial(
                    _site, manifest_store=manifest, pages_project=cfg.app.promote.pages_project
                )
                site_filenames = lambda: [p.lstrip("/") for p in manifest.load()]  # noqa: E731
            else:
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
        html_writer=html_writer,
        publish=publish,
        publish_site=publish_site,
        site_filenames=site_filenames,
        sync_promotions=sync,
        log_usage=log_usage,
        tag_store=tag_store,
        story_store=story_store,
        on_progress=on_progress or (lambda _msg: None),
        embedder=build_embedder(cfg),
        embedding_threshold=embedding_threshold(cfg),
    )
