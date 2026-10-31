"""FetchSource that reads the Cloudflare RSS Worker's D1 buffer.

The Worker polls feeds hourly; this only reads a window out of it. Unlike the
newsletter Worker there is no ACK — the buffer is retention, not a queue, so a
crashed digest simply reads the same window again.
"""

from __future__ import annotations

import html
import logging
import math
from datetime import UTC, datetime

import httpx

from cyris.domain.models import Article, Lane, SourceConfig

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30

# The Worker's own ceiling on `GET /articles` (`workers/rss/src/index.js`). A read
# needs a bound because each row carries the feed's full content and the Worker
# holds the whole result in memory; it is not the run cap, which `run_digest`
# applies after the store so no row is lost unstored. Sent by value because the
# Worker's default without one is lower.
WORKER_ROW_CEILING = 2000

# The Worker's own poll shape (`CONCURRENCY` and `FETCH_TIMEOUT_MS` in
# `workers/rss/src/index.js`): feeds are fetched four at a time, each batch
# waiting at most 20 seconds, so a poll of N feeds takes at most
# ceil(N / 4) * 20 seconds of fetching. The margin is the buffer writes and the
# prune after them, which run once per poll rather than per feed.
POLL_CONCURRENCY = 4
POLL_FEED_TIMEOUT_SECONDS = 20
POLL_WRITE_MARGIN_SECONDS = 30


def poll_timeout_seconds(feeds: int) -> float:
    """How long a `POST /poll` over `feeds` feeds may take before it is given up."""
    batches = max(1, math.ceil(feeds / POLL_CONCURRENCY))
    return batches * POLL_FEED_TIMEOUT_SECONDS + POLL_WRITE_MARGIN_SECONDS


def _worker_bound(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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
    ) -> list[Article]:
        """Read the window from D1 and map rows onto Articles.

        The bounds go out in `Date#toISOString()` form because the Worker compares them
        to each row's `fetched_at`, when the entry entered the buffer, as strings.
        """
        params = {
            "after": _worker_bound(after),
            "before": _worker_bound(before),
            "limit": WORKER_ROW_CEILING,
        }
        # A failed read raises rather than returning []: `fetch_all_articles` skips the
        # source and lists it in `failed_sources`, which the failed-fetch alert reads.
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            resp = await client.get(
                f"{self._worker_url}/articles",
                params=params,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        resp.raise_for_status()
        rows = resp.json()
        if len(rows) >= WORKER_ROW_CEILING:
            logger.warning(
                "RSS worker read hit its %d-row ceiling: the earliest-buffered rows in "
                "this window were left in the buffer and not stored",
                WORKER_ROW_CEILING,
            )

        articles = []
        for row in rows:
            source = sources.get(row.get("source_name", ""))
            try:
                articles.append(
                    Article(
                        id=row.get("guid") or row["url"],
                        # The Worker's parser, like feedparser, leaves a CDATA title's
                        # entities as text, and the page would escape them again.
                        title=html.unescape(row.get("title", "")),
                        url=row["url"],
                        content=row.get("content", ""),
                        author=row.get("author") or None,
                        published_at=row["published_at"],
                        source_name=row.get("source_name", ""),
                        source_lane=source.lane if source else Lane.SKIM,
                        source_tags=source.tags if source else [],
                        image_url=row.get("image_url") or None,
                    )
                )
            except (KeyError, ValueError):
                logger.warning("Skipping malformed row: %s", row.get("url"), exc_info=True)

        logger.info("Read %d buffered entries from the RSS worker", len(articles))
        return articles

    async def poll(self, feeds: int) -> None:
        """Ask the Worker to poll every feed in `sources` now, outside its hourly cron.

        The onboarding wizard's vote cards need the feeds a reader just picked,
        and the next cron tick can be an hour away. `feeds` is how many the
        Worker will poll, which bounds how long it may take. Raises on a failed
        or timed-out poll; the rows a poll stored stay, whatever the caller does.
        """
        async with httpx.AsyncClient(timeout=poll_timeout_seconds(feeds)) as client:
            resp = await client.post(
                f"{self._worker_url}/poll",
                headers={"Authorization": f"Bearer {self._token}"},
            )
        resp.raise_for_status()
        logger.info("RSS worker polled: %s", resp.json())

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
