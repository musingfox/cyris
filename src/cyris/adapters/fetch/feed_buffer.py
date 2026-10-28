"""The RSS Worker's job, in the local SQLite store.

`[rss] local_buffer = true` swaps the digest-time poll for the Worker's shape: one
hourly poll writes every feed entry into a table, and the digest reads the window
out of it. Same rules as `workers/rss/src/index.js` — `INSERT OR IGNORE`, so
re-seeing an entry changes nothing; retention pruned by `fetched_at`; entries a
feed dates past retention dropped before they reach the table, because a blog's
feed carries months of history and an old entry inserted now would reach the next
digest as new.

Without it, a digest-time poll sees only each feed's current snapshot: measured,
141 of 317 articles went missing (`docs/decisions/0001-rss-arrives-through-an-hourly-buffer.md`).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from cyris.adapters.fetch.rss_source import RssSource
from cyris.domain.models import Article, SourceConfig

logger = logging.getLogger(__name__)

# Same window as the Worker, counted from buffer entry rather than publication:
# "matches the ArticleStore's dedup scan window".
RETENTION_DAYS = 8

# The Worker's own read ceiling; every row carries a feed's full content.
ROW_CEILING = 2000

# Created on first use by the buffer that owns it, not by `schema.sql`: D1 does
# not need this table, and a shared `cyris-rss` database already has the Worker's.
DDL = """
CREATE TABLE IF NOT EXISTS feed_buffer (
  url          TEXT PRIMARY KEY,
  id           TEXT NOT NULL DEFAULT '',
  title        TEXT NOT NULL DEFAULT '',
  content      TEXT NOT NULL DEFAULT '',
  author       TEXT,
  published_at TEXT NOT NULL,
  source_name  TEXT NOT NULL,
  fetched_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feed_buffer_published_at ON feed_buffer(published_at);
"""

INSERT = """
INSERT OR IGNORE INTO feed_buffer
  (url, id, title, content, author, published_at, source_name, fetched_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

PRUNE = "DELETE FROM feed_buffer WHERE fetched_at < ?"

SELECT = """
SELECT url, id, title, content, author, published_at, source_name
FROM feed_buffer
WHERE published_at >= ? AND published_at < ?
  AND source_name IN ({names})
ORDER BY published_at DESC
LIMIT ?
"""

STATUS = "SELECT COUNT(*) AS rows, MAX(fetched_at) AS newest FROM feed_buffer"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _from_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class FeedBuffer:
    """Writes the hourly poll's entries; `cyris buffer` runs it, one poll per tick."""

    def __init__(self, client, fetcher: RssSource | None = None) -> None:
        self._client = client
        self._fetcher = fetcher or RssSource()
        self._client.query(DDL)

    def record(self, articles: list[Article], now: datetime) -> int:
        """Insert what is new; returns the rows the table did not already hold."""
        fetched_at = _iso(now)
        written = 0
        for article in articles:
            written += self._client.query(
                INSERT,
                [
                    article.url,
                    article.id,
                    article.title,
                    article.content,
                    article.author,
                    _iso(article.published_at),
                    article.source_name,
                    fetched_at,
                ],
            ).changes
        return written

    def prune(self, now: datetime) -> int:
        return self._client.query(PRUNE, [_iso(now - timedelta(days=RETENTION_DAYS))]).changes

    async def poll(self, sources: dict[str, SourceConfig], now: datetime) -> tuple[int, int]:
        """One poll: fetch every feed's current snapshot, write it, prune. (written, pruned)."""
        cutoff = now - timedelta(days=RETENTION_DAYS)
        # Exclusive upper bound; a feed dated a few seconds ahead of this clock is
        # still the feed's, so ask past `now`.
        articles = await self._fetcher.fetch_articles(cutoff, now + timedelta(seconds=1), sources)
        written = self.record(articles, now)
        pruned = self.prune(now)
        logger.info("Buffer: %d entries, %d new, %d pruned", len(articles), written, pruned)
        return written, pruned

    def status(self) -> tuple[int, str | None]:
        """Rows held and the newest `fetched_at`, for `doctor`."""
        row = self._client.query(STATUS).rows[0]
        return int(row["rows"] or 0), row["newest"]


class BufferedRssSource:
    """FetchSource over `feed_buffer`: a window read, no network at digest time."""

    def __init__(self, client) -> None:
        self._client = client

    async def fetch_articles(
        self,
        after: datetime,
        before: datetime,
        sources: dict[str, SourceConfig],
    ) -> list[Article]:
        feeds = {name: s for name, s in sources.items() if s.url and s.type == "rss"}
        if not feeds:
            return []

        names = list(feeds)
        # Two window bounds, then one placeholder per source name.
        sql = SELECT.format(names=", ".join("?" * len(names)))
        rows = self._client.query(sql, [_iso(after), _iso(before), *names, ROW_CEILING]).rows

        articles = []
        for row in rows:
            source = feeds[row["source_name"]]
            articles.append(
                Article(
                    id=row["id"],
                    title=row["title"],
                    url=row["url"],
                    content=row["content"],
                    author=row["author"],
                    published_at=_from_iso(row["published_at"]),
                    source_name=source.name,
                    source_lane=source.lane,
                    source_tags=source.tags,
                )
            )
        logger.info("Read %d buffered entries from %d feeds", len(articles), len(feeds))
        return articles

    async def health_check(self) -> bool:
        return True
