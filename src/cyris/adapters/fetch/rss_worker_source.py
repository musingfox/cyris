"""FetchSource that reads the Cloudflare RSS Worker's D1 buffer.

The Worker polls feeds hourly; this only reads a window out of it. Unlike the
newsletter Worker there is no ACK — the buffer is retention, not a queue, so a
crashed digest simply reads the same window again.
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

from cyris.domain.models import Article, SourceConfig, Tier

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30


class CloudflareRssSource:
    """Read feed entries buffered in the Cloudflare RSS Worker's D1."""

    def __init__(self, worker_url: str, token: str) -> None:
        self._worker_url = worker_url.rstrip("/")
        self._token = token

    async def fetch_articles(
        self,
        after: datetime,
        before: datetime,
        sources: dict[str, SourceConfig],
        limit: int = 200,
    ) -> list[Article]:
        """Read the window from D1 and map rows onto Articles."""
        params = {
            "after": after.isoformat(),
            "before": before.isoformat(),
            "limit": limit,
        }
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                resp = await client.get(
                    f"{self._worker_url}/articles",
                    params=params,
                    headers={"Authorization": f"Bearer {self._token}"},
                )
            resp.raise_for_status()
            rows = resp.json()
        except (httpx.HTTPError, ValueError):
            logger.warning("RSS worker read failed", exc_info=True)
            return []

        articles = []
        for row in rows:
            source = sources.get(row.get("source_name", ""))
            try:
                articles.append(
                    Article(
                        id=row.get("guid") or row["url"],
                        title=row.get("title", ""),
                        url=row["url"],
                        content=row.get("content", ""),
                        author=row.get("author") or None,
                        published_at=row["published_at"],
                        source_name=row.get("source_name", ""),
                        source_tier=source.tier if source else Tier.FILTER,
                        source_tags=source.tags if source else [],
                    )
                )
            except (KeyError, ValueError):
                logger.warning("Skipping malformed row: %s", row.get("url"), exc_info=True)

        logger.info("Read %d buffered entries from the RSS worker", len(articles))
        return articles

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                resp = await client.get(
                    f"{self._worker_url}/stats",
                    headers={"Authorization": f"Bearer {self._token}"},
                )
            return resp.status_code == 200
        except httpx.HTTPError:
            return False
