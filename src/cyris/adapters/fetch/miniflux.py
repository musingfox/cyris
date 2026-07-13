"""Miniflux API client for fetching RSS entries."""

import asyncio
import logging
from datetime import datetime

import miniflux

from cyris.adapters.fetch.extractor import extract_full_text
from cyris.adapters.fetch.source_matcher import SourceMatcher
from cyris.adapters.http_client import HttpClient
from cyris.domain.models import Article, SourceConfig, Tier

logger = logging.getLogger(__name__)


class MinifluxClient:
    """Async wrapper around the synchronous miniflux Python client."""

    def __init__(self, url: str, api_key: str) -> None:
        self._client = miniflux.Client(url, api_key=api_key)
        self._http_client = HttpClient()

    async def fetch_entries(
        self,
        after: datetime,
        before: datetime,
        sources: dict[str, SourceConfig],
        aliases: dict[str, str] | None = None,
        status: str = "",
        limit: int = 200,
        cookies: dict[str, str] | None = None,
    ) -> list[Article]:
        """Fetch unread entries from Miniflux within a time window.

        Args:
            after: Start of time window (inclusive).
            before: End of time window (exclusive).
            sources: Source configs keyed by name, used for tier/tag lookup.
            aliases: Optional feed title → source name mappings.
            status: Entry status filter. Defaults to "unread".
            limit: Max entries to fetch. Defaults to 200.
            cookies: Optional cookies for paywall sources.

        Returns:
            List of Article models mapped from Miniflux entries.
        """
        after_ts = int(after.timestamp())
        before_ts = int(before.timestamp())

        params: dict[str, object] = {
            "limit": limit,
            "after": after_ts,
            "before": before_ts,
            "order": "published_at",
            "direction": "desc",
        }
        if status:
            params["status"] = status

        result = await asyncio.to_thread(
            self._client.get_entries,
            **params,
        )

        entries = result.get("entries", [])
        logger.info("Fetched %d entries from Miniflux", len(entries))

        matcher = SourceMatcher(sources, alias_map=aliases)
        articles = []
        for entry in entries:
            feed_title = entry.get("feed", {}).get("title", "")
            source_cfg = matcher.match(feed_title)

            # For paywall sources with cookies, fetch full text
            content = entry.get("content", "")
            if source_cfg and source_cfg.paywall and cookies:
                try:
                    extracted = await extract_full_text(
                        entry.get("url", ""),
                        self._http_client,
                        cookies=cookies,
                    )
                    if extracted.content:
                        content = extracted.content
                        logger.debug("Extracted full text for paywall source: %s", feed_title)
                except Exception:
                    logger.warning(
                        "Failed to extract full text for %s", entry.get("url", ""), exc_info=True
                    )

            article = Article(
                id=entry["id"],
                title=entry.get("title", ""),
                url=entry.get("url", ""),
                content=content,
                author=entry.get("author") or None,
                published_at=entry["published_at"],
                source_name=feed_title,
                source_tier=source_cfg.tier if source_cfg else Tier.FILTER,
                source_tags=source_cfg.tags if source_cfg else [],
            )
            articles.append(article)

        return articles

    async def mark_as_read(self, entry_ids: list[int]) -> None:
        """Mark entries as read in Miniflux.

        Args:
            entry_ids: List of Miniflux entry IDs to mark as read.
        """
        if not entry_ids:
            return
        await asyncio.to_thread(self._client.update_entries, entry_ids, "read")
        logger.info("Marked %d entries as read", len(entry_ids))
