"""Miniflux FetchSource adapter."""

from __future__ import annotations

import logging
from datetime import datetime

from cyris.adapters.fetch.miniflux import MinifluxClient
from cyris.domain.models import Article, SourceConfig

logger = logging.getLogger(__name__)


class MinifluxSource:
    """FetchSource adapter for Miniflux RSS reader."""

    def __init__(self, client: MinifluxClient) -> None:
        self._client = client

    async def fetch_articles(
        self,
        after: datetime,
        before: datetime,
        sources: dict[str, SourceConfig],
        aliases: dict[str, str] | None = None,
        limit: int = 200,
        cookies: dict[str, str] | None = None,
    ) -> list[Article]:
        """Fetch articles from Miniflux.

        Delegates to MinifluxClient.fetch_entries.
        """
        return await self._client.fetch_entries(
            after=after,
            before=before,
            sources=sources,
            aliases=aliases,
            limit=limit,
            cookies=cookies,
        )

    async def mark_as_read(self, article_ids: list[int | str]) -> None:
        """Mark articles as read in Miniflux.

        Filters to int IDs only (Miniflux requires int IDs).
        """
        int_ids = []
        for article_id in article_ids:
            if isinstance(article_id, int):
                int_ids.append(article_id)
            elif isinstance(article_id, str) and article_id.isdigit():
                int_ids.append(int(article_id))
        if int_ids:
            await self._client.mark_as_read(int_ids)

    async def health_check(self) -> bool:
        """Check if Miniflux is reachable.

        Attempts to fetch zero entries to test connection.
        """
        try:
            from datetime import UTC

            now = datetime.now(UTC)
            await self._client.fetch_entries(
                after=now,
                before=now,
                sources={},
                limit=0,
            )
            return True
        except Exception:
            logger.warning("Miniflux health check failed", exc_info=True)
            return False
