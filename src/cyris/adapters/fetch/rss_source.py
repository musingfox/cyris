"""FetchSource that polls RSS/Atom feeds directly, no Miniflux in between.

ponytail: not wired into bootstrap. Built to test whether Miniflux could just be
deleted; the measured answer was no. A digest-time poll sees only what a feed's
current snapshot holds, and high-volume feeds hold 2-4h against a 24h window —
141 of 317 articles went missing. What Miniflux provides is *accumulation*, not
parsing, and workers/rss/ (hourly poll into D1) is what replaces it. This stays
as the local fallback for when MinifluxSource is retired: same shape, no
accumulation, correct only for feeds whose snapshot outlives the window.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import feedparser

from cyris.adapters.fetch.email_parser import strip_tracking_params
from cyris.adapters.http_client import HttpClient
from cyris.domain.models import Article, SourceConfig, Tier

logger = logging.getLogger(__name__)

# ponytail: fixed fan-out cap; make it configurable only if a run actually stalls
MAX_CONCURRENT_FEEDS = 10


def _entry_published(entry: object) -> datetime | None:
    """Return a tz-aware publish time, or None when the entry has no usable date.

    feedparser normalises `*_parsed` to UTC but hands back a naive struct_time;
    comparing that against the tz-aware digest window raises TypeError.
    """
    for key in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, key, None)
        if parsed:
            return datetime(*parsed[:6], tzinfo=UTC)
    return None


class RssSource:
    """FetchSource that fetches and parses feed URLs from sources.yaml."""

    def __init__(self, http_client: HttpClient | None = None) -> None:
        self._http = http_client or HttpClient()

    async def fetch_articles(
        self,
        after: datetime,
        before: datetime,
        sources: dict[str, SourceConfig],
        aliases: dict[str, str] | None = None,
        limit: int = 200,
    ) -> list[Article]:
        """Fetch every configured feed concurrently and window-filter the entries.

        `aliases` is accepted for FetchSource compatibility but unused: entries are
        keyed by the feed URL they came from, so there is no feed-title to resolve.
        """
        feeds = [s for s in sources.values() if s.url and s.type == "rss"]
        if not feeds:
            return []

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_FEEDS)

        async def one(source: SourceConfig) -> list[Article]:
            async with semaphore:
                return await self._fetch_feed(source, after, before)

        results = await asyncio.gather(*(one(s) for s in feeds), return_exceptions=True)

        articles: list[Article] = []
        for source, result in zip(feeds, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("Feed failed: %s (%s): %s", source.name, source.url, result)
                continue
            articles.extend(result)

        # Miniflux applied `limit` server-side across all feeds; per-feed limiting
        # here would let the total balloon, so truncate the merged newest-first list.
        articles.sort(key=lambda a: a.published_at, reverse=True)
        logger.info("Fetched %d entries from %d feeds", len(articles), len(feeds))
        return articles[:limit]

    async def _fetch_feed(
        self,
        source: SourceConfig,
        after: datetime,
        before: datetime,
    ) -> list[Article]:
        response = await self._http.get(source.url or "")
        response.raise_for_status()
        # feedparser.parse(url) would do its own blocking urllib fetch — hand it bytes
        # so the shared client's User-Agent and redirect handling apply.
        parsed = await asyncio.to_thread(feedparser.parse, response.content)

        articles = []
        for entry in parsed.entries:
            published_at = _entry_published(entry)
            if published_at is None:
                logger.debug("Entry without date skipped: %s", getattr(entry, "link", ""))
                continue
            if not (after <= published_at < before):
                continue

            # Miniflux served these already-stripped; without this the same article
            # arrives under two URLs and defeats the store's URL primary key.
            url = strip_tracking_params(getattr(entry, "link", ""))
            if not url:
                continue

            content = _entry_content(entry)

            articles.append(
                Article(
                    id=getattr(entry, "id", "") or url,
                    title=getattr(entry, "title", ""),
                    url=url,
                    content=content,
                    author=getattr(entry, "author", None) or None,
                    published_at=published_at,
                    source_name=source.name,
                    source_tier=source.tier or Tier.FILTER,
                    source_tags=source.tags,
                )
            )
        return articles

    async def mark_as_read(self, article_ids: list[int | str]) -> None:
        """No-op: read state lives in the ArticleStore, not in the feed."""

    async def health_check(self) -> bool:
        return True


def _entry_content(entry: object) -> str:
    """Prefer full content over the summary, mirroring what Miniflux served."""
    content = getattr(entry, "content", None)
    if content:
        return content[0].get("value", "")
    return getattr(entry, "summary", "")
