"""Newsletter archive FetchSource adapter."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from cyris.domain.models import Article, SourceConfig

logger = logging.getLogger(__name__)


class NewsletterArchiveSource:
    """FetchSource adapter for pre-populated newsletter archive files."""

    def __init__(self, archive_path: Path) -> None:
        """Initialize newsletter archive source.

        Args:
            archive_path: Path to directory containing newsletter archive JSON files.
        """
        self.archive_path = archive_path

    async def fetch_articles(
        self,
        after: datetime,
        before: datetime,
        sources: dict[str, SourceConfig],
        aliases: dict[str, str] | None = None,
        limit: int = 200,
    ) -> list[Article]:
        """Load newsletter articles from archive within time window.

        Args:
            after: Start of time window (inclusive).
            before: End of time window (exclusive).
            sources: Source configs (unused for archive, kept for protocol).
            aliases: Aliases (unused for archive, kept for protocol).
            limit: Max articles (unused for archive, kept for protocol).

        Returns:
            List of Article models loaded from archive files.
        """
        if not self.archive_path.exists():
            logger.warning("Newsletter archive path does not exist: %s", self.archive_path)
            return []

        articles = []
        for f in sorted(self.archive_path.glob("*.json")):
            try:
                data = json.loads(f.read_text())
                for item in data:
                    article = Article.model_validate(item)
                    if after <= article.published_at <= before:
                        articles.append(article)
            except Exception:
                logger.warning("Failed to load newsletter archive %s", f, exc_info=True)

        logger.info("Loaded %d articles from newsletter archive", len(articles))
        return articles

    async def mark_as_read(self, article_ids: list[int | str]) -> None:
        """No-op for newsletter archive (read-only source)."""
        pass

    async def health_check(self) -> bool:
        """Check if archive path exists.

        Returns:
            True if archive path exists, False otherwise.
        """
        return self.archive_path.exists()
