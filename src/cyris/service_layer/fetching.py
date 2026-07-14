"""Unified article fetching from all sources."""

import logging
from datetime import datetime

from cyris.domain.models import Article, SourceConfig
from cyris.service_layer.ports import FetchSource

logger = logging.getLogger(__name__)


async def fetch_all_articles(
    fetch_sources: list[FetchSource],
    after: datetime,
    before: datetime,
    sources: dict[str, SourceConfig],
    aliases: dict[str, str] | None = None,
    limit: int = 200,
    cookies: dict[str, str] | None = None,
) -> tuple[list[Article], list[str]]:
    """Fetch articles from multiple sources, deduplicate by URL.

    Args:
        fetch_sources: List of FetchSource implementations (e.g., Miniflux, newsletters).
        after: Start of time window (inclusive).
        before: End of time window (exclusive).
        sources: Source configs keyed by name.
        aliases: Optional feed title → source name mappings.
        limit: Max articles per source. Defaults to 200.
        cookies: Optional cookies for paywall sources.

    Returns:
        Tuple of (deduplicated articles — last source wins, names of sources that failed).
    """
    by_url: dict[str, Article] = {}
    failed_sources: list[str] = []

    for source in fetch_sources:
        try:
            articles = await source.fetch_articles(
                after=after,
                before=before,
                sources=sources,
                aliases=aliases,
                limit=limit,
                cookies=cookies,
            )
            # Deduplicate by URL (last source wins)
            for article in articles:
                by_url[article.url] = article
        except Exception as e:
            # ponytail: log the message, not the stack — this failure is handled (the
            # source is skipped and the pipeline degrades gracefully). A leaked traceback
            # makes a handled skip look fatal. Run with --verbose for the stack.
            logger.warning(
                "Failed to fetch from source %s: %s (check its URL/credentials)",
                type(source).__name__,
                e,
                exc_info=logger.isEnabledFor(logging.DEBUG),
            )
            failed_sources.append(type(source).__name__)
            continue

    logger.info("Fetched %d unique articles from %d sources", len(by_url), len(fetch_sources))
    return list(by_url.values()), failed_sources
