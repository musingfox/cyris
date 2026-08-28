"""Persistent article store with URL-based deduplication."""

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cyris.domain.models import Article, ArticleState, SaveResult, StoredArticle

logger = logging.getLogger(__name__)


class ArticleStore:
    """Persistent storage for articles with state management.

    Articles are partitioned by date into JSON files. Deduplication is based on URL.
    """

    def __init__(self, vault_path: Path) -> None:
        self.articles_path = vault_path / "articles"
        self.articles_path.mkdir(parents=True, exist_ok=True)

    def _partition_path(self, dt: datetime) -> Path:
        """Get partition file path for a given date."""
        return self.articles_path / f"{dt.strftime('%Y-%m-%d')}.json"

    def _load_partition(self, path: Path) -> list[StoredArticle]:
        """Load articles from a partition file."""
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
            return [StoredArticle.model_validate(item) for item in data]
        except (json.JSONDecodeError, ValueError) as e:
            logger.error("Failed to load partition %s: %s", path, e)
            return []

    def _save_partition(self, path: Path, articles: list[StoredArticle]) -> None:
        """Save articles to a partition file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        serialized = [a.model_dump(mode="json") for a in articles]
        path.write_text(json.dumps(serialized, ensure_ascii=False, indent=2))

    def save(self, articles: list[Article], now: datetime | None = None) -> SaveResult:
        """Save new articles to store with deduplication by URL.

        Args:
            articles: Articles to save.
            now: Timestamp for first_seen_at (defaults to UTC now).

        Returns:
            SaveResult with saved and skipped counts.
        """
        if now is None:
            now = datetime.now(UTC)

        if not articles:
            return SaveResult(saved_count=0, skipped_count=0)

        # Build URL index from recent partitions for dedup (today + last 7 days)
        existing_urls: set[str] = set()
        for i in range(8):
            scan_date = now.date() - timedelta(days=i)
            scan_path = self._partition_path(datetime.combine(scan_date, datetime.min.time()))
            partition = self._load_partition(scan_path)
            existing_urls.update(a.url for a in partition)

        # Load today's partition
        today_path = self._partition_path(now)
        today_articles = self._load_partition(today_path)

        # Filter out duplicates and convert to StoredArticle
        new_articles = []
        skipped = 0

        for article in articles:
            if article.url in existing_urls:
                skipped += 1
                continue

            stored = StoredArticle.from_article(article, first_seen_at=now)
            new_articles.append(stored)
            existing_urls.add(article.url)

        # Append to today's partition
        if new_articles:
            today_articles.extend(new_articles)
            self._save_partition(today_path, today_articles)
            logger.info("Saved %d new articles to %s", len(new_articles), today_path)

        return SaveResult(saved_count=len(new_articles), skipped_count=skipped)

    def update_states(
        self, url_to_state: dict[str, tuple[ArticleState, str | None]], digest_date: str
    ) -> int:
        """Update article states after digest processing.

        Args:
            url_to_state: Mapping of URL to (state, rejection_reason).
            digest_date: Digest date in YYYY-MM-DD format.

        Returns:
            Count of updated articles.
        """
        # Validate digest_date format
        try:
            datetime.strptime(digest_date, "%Y-%m-%d")
        except ValueError as e:
            raise ValueError(f"Invalid digest_date format: {digest_date}") from e

        if not url_to_state:
            return 0

        # Scan recent partitions (last 7 days from digest_date, plus today if in past)
        digest_dt = datetime.strptime(digest_date, "%Y-%m-%d")
        now = datetime.now(UTC)
        updated_count = 0
        modified_partitions: dict[Path, list[StoredArticle]] = {}

        # Collect all dates to scan
        dates_to_scan = []
        for i in range(8):
            scan_date = digest_dt.date() - timedelta(days=i)
            dates_to_scan.append(scan_date)

        # Also include today if it's not already covered
        today = now.date()
        if today not in dates_to_scan:
            dates_to_scan.append(today)

        for scan_date in dates_to_scan:
            scan_path = self._partition_path(datetime.combine(scan_date, datetime.min.time()))

            if not scan_path.exists():
                continue

            articles = self._load_partition(scan_path)
            modified = False

            for article in articles:
                if article.url in url_to_state:
                    if article.triaged_at is not None:
                        continue  # a human vote outranks the pipeline's verdict
                    state, reason = url_to_state[article.url]
                    article.state = state
                    article.digest_date = digest_date
                    article.rejection_reason = reason
                    updated_count += 1
                    modified = True

            if modified:
                modified_partitions[scan_path] = articles

        # Write back modified partitions
        for path, articles in modified_partitions.items():
            self._save_partition(path, articles)

        logger.info("Updated states for %d articles", updated_count)
        return updated_count

    def load_by_time_range(
        self,
        start: datetime,
        end: datetime,
        state_filter: ArticleState | None = None,
    ) -> list[StoredArticle]:
        """Load articles within a time range, optionally filtered by state.

        Args:
            start: Start of time range (inclusive).
            end: End of time range (exclusive).
            state_filter: Optional state to filter by.

        Returns:
            List of StoredArticles sorted by first_seen_at ascending.
        """
        results = []

        # Normalize to UTC-aware datetimes to avoid naive/aware comparison errors
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)

        # Generate date range
        current_date = start.date()
        end_date = end.date()

        while current_date <= end_date:
            partition_dt = datetime.combine(current_date, datetime.min.time())
            partition_path = self._partition_path(partition_dt)
            articles = self._load_partition(partition_path)

            for article in articles:
                # Normalize article timestamp
                first_seen = article.first_seen_at
                if first_seen.tzinfo is None:
                    first_seen = first_seen.replace(tzinfo=UTC)

                # Filter by time range
                if first_seen < start or first_seen >= end:
                    continue

                # Filter by state if specified
                if state_filter is not None and article.state != state_filter:
                    continue

                results.append(article)

            current_date += timedelta(days=1)

        # Sort by first_seen_at ascending
        results.sort(key=lambda a: a.first_seen_at)
        return results

    def list_articles(
        self,
        state: ArticleState | list[ArticleState] | None = None,
        limit: int = 100,
        offset: int = 0,
        sort_by: str = "first_seen_at",
        descending: bool = True,
    ) -> list[StoredArticle]:
        """List articles with filtering, sorting, and pagination.

        Args:
            state: Filter by state(s); None = all states.
            limit: Maximum articles to return.
            offset: Number of articles to skip.
            sort_by: Sort field: "first_seen_at" | "published_at" | "title".
            descending: Sort order (True = newest/Z-A first, False = oldest/A-Z first).

        Returns:
            List of StoredArticles.

        Raises:
            ValueError: If sort_by is not a valid field.
        """
        valid_sort_fields = {"first_seen_at", "published_at", "title", "score", "language"}
        if sort_by not in valid_sort_fields:
            raise ValueError(f"Invalid sort_by field: {sort_by}")

        # Normalize state to set for filtering
        state_filter: set[ArticleState] | None = None
        if state is not None:
            state_filter = set(state) if isinstance(state, list) else {state}

        # Load all articles from all partition files
        all_articles = []
        for partition_path in sorted(self.articles_path.glob("*.json")):
            articles = self._load_partition(partition_path)
            all_articles.extend(articles)

        # Apply state filter
        if state_filter is not None:
            all_articles = [a for a in all_articles if a.state in state_filter]

        # Sort
        def _sort_key(a: StoredArticle) -> tuple:
            val = getattr(a, sort_by)
            if sort_by == "score":
                # None scores always go to end (use large number for asc, small for desc)
                if val is None:
                    return (float("inf"),) if not descending else (float("-inf"),)
                return (val,)
            if sort_by == "language":
                # Chinese first: zh=0, en=1, None=2
                lang_order = {"zh": 0, "en": 1}
                return (lang_order.get(val, 2),)
            return (val,)

        reverse = descending
        all_articles.sort(key=_sort_key, reverse=reverse)

        # Apply offset and limit
        start_idx = offset
        end_idx = offset + limit
        return all_articles[start_idx:end_idx]

    def get_by_urls(self, urls: list[str]) -> list[StoredArticle]:
        """Retrieve articles by their URLs.

        Args:
            urls: List of article URLs to look up.

        Returns:
            List of matching StoredArticles (order not guaranteed).
        """
        url_set = set(urls)
        results = []
        for partition_path in self.articles_path.glob("*.json"):
            articles = self._load_partition(partition_path)
            for article in articles:
                if article.url in url_set:
                    results.append(article)
        return results

    def update_article_state(
        self,
        url: str,
        state: ArticleState,
        reason: str | None = None,
    ) -> bool:
        """Update state of a single article by URL.

        Args:
            url: Article URL.
            state: New state.
            reason: Rejection reason (for REJECTED state).

        Returns:
            True if updated, False if URL not found.
        """
        from datetime import date

        today_str = date.today().isoformat()

        # Scan all partition files
        for partition_path in self.articles_path.glob("*.json"):
            articles = self._load_partition(partition_path)
            modified = False

            for article in articles:
                if article.url == url:
                    article.state = state
                    article.digest_date = today_str
                    article.rejection_reason = reason
                    if state == ArticleState.PENDING:
                        # Undoing a triage drops the human stamp too, otherwise the
                        # update_states guard would keep the row from ever being judged.
                        article.triaged_at = None
                    modified = True
                    break

            if modified:
                self._save_partition(partition_path, articles)
                logger.info("Updated state for %s to %s", url, state)
                return True

        return False

    def accept(self, urls: list[str]) -> int:
        """Mark articles as accepted. Returns count of updated articles."""
        return sum(self.update_article_state(url, ArticleState.ACCEPTED) for url in urls)

    def reject(self, urls: list[str], reason: str) -> int:
        """Mark articles as rejected with a reason. Returns count of updated articles."""
        return sum(
            self.update_article_state(url, ArticleState.REJECTED, reason=reason) for url in urls
        )

    def reset_to_pending(self, url: str) -> bool:
        """Return an article to the pending state (undo a triage decision)."""
        return self.update_article_state(url, ArticleState.PENDING)

    def update_scores(
        self,
        url_to_score_lang: dict[str, tuple[float, str]],
        scan_days: int = 30,
    ) -> int:
        """Update article scores and language across partitions.

        Args:
            url_to_score_lang: Mapping of URL to (score, language).
            scan_days: Number of days of partitions to scan.

        Returns:
            Count of updated articles.
        """
        if not url_to_score_lang:
            return 0

        updated = 0
        today = datetime.now(UTC).date()

        for day_offset in range(scan_days):
            date = today - timedelta(days=day_offset)
            partition_file = self._partition_path(datetime.combine(date, datetime.min.time()))
            if not partition_file.exists():
                continue

            articles = self._load_partition(partition_file)
            modified = False

            for article in articles:
                if article.url in url_to_score_lang:
                    score, language = url_to_score_lang[article.url]
                    article.score = score
                    article.language = language
                    modified = True
                    updated += 1

            if modified:
                self._save_partition(partition_file, articles)

        return updated

    def update_triage_timestamp(self, urls: list[str], triaged_at: datetime) -> int:
        """Update triaged_at timestamp for articles.

        Args:
            urls: List of article URLs.
            triaged_at: Timestamp when articles were triaged.

        Returns:
            Count of updated articles.
        """
        if not urls:
            return 0

        url_set = set(urls)
        updated = 0
        modified_partitions: dict[Path, list[StoredArticle]] = {}

        for partition_path in self.articles_path.glob("*.json"):
            articles = self._load_partition(partition_path)
            modified = False

            for article in articles:
                if article.url in url_set:
                    article.triaged_at = triaged_at
                    modified = True
                    updated += 1

            if modified:
                modified_partitions[partition_path] = articles

        for path, articles in modified_partitions.items():
            self._save_partition(path, articles)

        logger.info("Updated triage timestamps for %d articles", updated)
        return updated

    def count_by_state(self) -> dict[ArticleState, int]:
        """Count articles grouped by state across all partitions.

        Returns:
            Dictionary mapping ArticleState to count.
        """
        counts: dict[ArticleState, int] = {}
        for partition_path in sorted(self.articles_path.glob("*.json")):
            articles = self._load_partition(partition_path)
            for article in articles:
                state = article.state or ArticleState.PENDING
                counts[state] = counts.get(state, 0) + 1
        return counts

    def delete_articles(
        self,
        state: ArticleState | list[ArticleState],
        older_than_days: int | None = None,
    ) -> int:
        """Delete articles matching state and age criteria.

        Args:
            state: State(s) to delete.
            older_than_days: Only delete if first_seen_at older than N days.

        Returns:
            Count of deleted articles.

        Raises:
            ValueError: If older_than_days < 0.
        """
        if older_than_days is not None and older_than_days < 0:
            raise ValueError("older_than_days must be non-negative")

        # Normalize state to set
        state_filter: set[ArticleState] = set(state) if isinstance(state, list) else {state}

        # Calculate cutoff date
        cutoff_date: datetime | None = None
        if older_than_days is not None:
            cutoff_date = datetime.now(UTC) - timedelta(days=older_than_days)

        deleted_count = 0
        modified_partitions: dict[Path, list[StoredArticle]] = {}

        # Scan all partition files
        for partition_path in self.articles_path.glob("*.json"):
            articles = self._load_partition(partition_path)
            remaining_articles = []

            for article in articles:
                # Check if article matches deletion criteria
                if article.state not in state_filter:
                    remaining_articles.append(article)
                    continue

                if article.triaged_at is not None:
                    remaining_articles.append(article)
                    continue

                # Check age if specified
                if cutoff_date is not None:
                    first_seen = article.first_seen_at
                    if first_seen.tzinfo is None:
                        first_seen = first_seen.replace(tzinfo=UTC)
                    if first_seen >= cutoff_date:
                        remaining_articles.append(article)
                        continue

                # Article matches deletion criteria
                deleted_count += 1

            # Save modified partition if articles were deleted
            if len(remaining_articles) < len(articles):
                modified_partitions[partition_path] = remaining_articles

        # Write back modified partitions (or delete if empty)
        for path, articles in modified_partitions.items():
            if articles:
                self._save_partition(path, articles)
            else:
                path.unlink()
                logger.info("Deleted empty partition file: %s", path)

        if deleted_count > 0:
            logger.info("Deleted %d articles", deleted_count)

        return deleted_count
