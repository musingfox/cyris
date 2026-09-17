"""Unified article fetching from all sources."""

import logging
from datetime import datetime

from cyris.domain.models import NEWSLETTER_SOURCE_TYPE, Article, SourceConfig
from cyris.service_layer.ports import FetchSource

logger = logging.getLogger(__name__)


async def fetch_all_articles(
    fetch_sources: list[FetchSource],
    after: datetime,
    before: datetime,
    sources: dict[str, SourceConfig],
    limit: int = 200,
) -> tuple[list[Article], list[str]]:
    """Fetch articles from multiple sources, deduplicate by URL.

    Args:
        fetch_sources: List of FetchSource implementations (RSS buffer, newsletters).
        after: Start of time window (inclusive).
        before: End of time window (exclusive).
        sources: Source configs keyed by name.
        limit: Max articles per source. Defaults to 200.

    Two newsletter issues with different ids that share a URL are both kept: a
    sender's repeated nav link must not swallow an issue before the store, which
    decides how to key the second one.

    Returns:
        Tuple of (deduplicated articles — last source wins, names of sources that failed).
    """
    by_url: dict[str, Article] = {}
    siblings: dict[int | str, Article] = {}
    failed_sources: list[str] = []

    for source in fetch_sources:
        try:
            articles = await source.fetch_articles(
                after=after,
                before=before,
                sources=sources,
                limit=limit,
            )
            # Deduplicate by URL (last source wins)
            for article in articles:
                held = by_url.get(article.url)
                if (
                    held is not None
                    and held.id != article.id
                    and held.source_type == article.source_type == NEWSLETTER_SOURCE_TYPE
                ):
                    siblings[article.id] = article
                else:
                    siblings.pop(article.id, None)
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

    unique = [*by_url.values(), *siblings.values()]
    logger.info("Fetched %d unique articles from %d sources", len(unique), len(fetch_sources))
    return unique, failed_sources
