"""Tests for triage feedback collection."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cyris.adapters.store.article_store import ArticleStore
from cyris.domain.models import ArticleState, StoredArticle, Tier
from cyris.learn.triage_feedback import collect_triage_feedback


class TestCollectTriageFeedback:
    """Tests for collect_triage_feedback function."""

    def test_collect_within_range(self, tmp_path: Path):
        """Collect 5 accepted + 2 rejected within range."""
        store = ArticleStore(tmp_path)
        now = datetime.now(UTC)

        # Create test articles
        accepted_articles = []
        for i in range(5):
            article = StoredArticle(
                url=f"https://example.com/accepted/{i}",
                original_id=i,
                title=f"Accepted Article {i}",
                content="test content",
                published_at=now - timedelta(hours=i),
                source_name="TestSource",
                source_tier=Tier.SUMMARIZE,
                first_seen_at=now - timedelta(hours=i),
                state=ArticleState.ACCEPTED,
                triaged_at=now - timedelta(days=i),
            )
            accepted_articles.append(article)

        rejected_articles = []
        for i in range(2):
            article = StoredArticle(
                url=f"https://example.com/rejected/{i}",
                original_id=100 + i,
                title=f"Rejected Article {i}",
                content="test content",
                published_at=now - timedelta(hours=i),
                source_name="TestSource",
                source_tier=Tier.FILTER,
                first_seen_at=now - timedelta(hours=i),
                state=ArticleState.REJECTED,
                triaged_at=now - timedelta(days=i),
            )
            rejected_articles.append(article)

        # Save articles to store manually
        today_path = store._partition_path(now)
        all_articles = accepted_articles + rejected_articles
        store._save_partition(today_path, all_articles)

        # Collect feedback
        feedback = collect_triage_feedback(store, days=14)

        assert feedback.accepted_count == 5
        assert feedback.rejected_count == 2
        assert len(feedback.accepted_articles) == 5
        assert len(feedback.rejected_articles) == 2

    def test_insufficient_triaged(self, tmp_path: Path):
        """Raise ValueError when total triaged < min_triaged."""
        store = ArticleStore(tmp_path)
        now = datetime.now(UTC)

        # Create only 2 articles (below minimum of 3)
        articles = [
            StoredArticle(
                url=f"https://example.com/{i}",
                original_id=i,
                title=f"Article {i}",
                content="test content",
                published_at=now,
                source_name="TestSource",
                source_tier=Tier.SUMMARIZE,
                first_seen_at=now,
                state=ArticleState.ACCEPTED,
                triaged_at=now,
            )
            for i in range(2)
        ]

        today_path = store._partition_path(now)
        store._save_partition(today_path, articles)

        with pytest.raises(ValueError, match="Insufficient triage feedback"):
            collect_triage_feedback(store, days=14, min_triaged=3)

    def test_exclude_outside_range(self, tmp_path: Path):
        """Exclude articles outside the days range."""
        store = ArticleStore(tmp_path)
        now = datetime.now(UTC)

        # Create articles: 3 within range, 2 outside range
        within_range = [
            StoredArticle(
                url=f"https://example.com/within/{i}",
                original_id=i,
                title=f"Within Range {i}",
                content="test content",
                published_at=now - timedelta(days=i),
                source_name="TestSource",
                source_tier=Tier.SUMMARIZE,
                first_seen_at=now - timedelta(days=i),
                state=ArticleState.ACCEPTED,
                triaged_at=now - timedelta(days=i),
            )
            for i in range(3)
        ]

        outside_range = [
            StoredArticle(
                url=f"https://example.com/outside/{i}",
                original_id=100 + i,
                title=f"Outside Range {i}",
                content="test content",
                published_at=now - timedelta(days=20 + i),
                source_name="TestSource",
                source_tier=Tier.SUMMARIZE,
                first_seen_at=now - timedelta(days=20 + i),
                state=ArticleState.ACCEPTED,
                triaged_at=now - timedelta(days=20 + i),
            )
            for i in range(2)
        ]

        # Save to different partitions
        for article in within_range + outside_range:
            partition_path = store._partition_path(article.first_seen_at)
            existing = store._load_partition(partition_path)
            existing.append(article)
            store._save_partition(partition_path, existing)

        # Collect feedback with 7-day window
        feedback = collect_triage_feedback(store, days=7)

        # Should only get articles within 7 days
        assert feedback.accepted_count == 3
        assert all("within" in a.url for a in feedback.accepted_articles)

    def test_ignores_untriaged(self, tmp_path: Path):
        """Articles without triaged_at are pipeline verdicts, not human feedback."""
        store = ArticleStore(tmp_path)
        now = datetime.now(UTC)

        # Create articles without triaged_at
        articles = [
            StoredArticle(
                url=f"https://example.com/{i}",
                original_id=i,
                title=f"Article {i}",
                content="test content",
                published_at=now - timedelta(days=i),
                source_name="TestSource",
                source_tier=Tier.SUMMARIZE,
                first_seen_at=now - timedelta(days=i),
                state=ArticleState.ACCEPTED,
                triaged_at=None,  # No triaged_at timestamp
            )
            for i in range(3)
        ]

        today_path = store._partition_path(now)
        store._save_partition(today_path, articles)

        with pytest.raises(ValueError, match="Insufficient triage feedback"):
            collect_triage_feedback(store, days=14)

    def test_mixed_states(self, tmp_path: Path):
        """Only collect ACCEPTED and REJECTED, ignore PENDING and AWAITING_TRIAGE."""
        store = ArticleStore(tmp_path)
        now = datetime.now(UTC)

        articles = [
            StoredArticle(
                url="https://example.com/accepted1",
                original_id=1,
                title="Accepted 1",
                content="test",
                published_at=now,
                source_name="Source",
                source_tier=Tier.SUMMARIZE,
                first_seen_at=now,
                state=ArticleState.ACCEPTED,
                triaged_at=now,
            ),
            StoredArticle(
                url="https://example.com/accepted2",
                original_id=2,
                title="Accepted 2",
                content="test",
                published_at=now,
                source_name="Source",
                source_tier=Tier.SUMMARIZE,
                first_seen_at=now,
                state=ArticleState.ACCEPTED,
                triaged_at=now,
            ),
            StoredArticle(
                url="https://example.com/rejected",
                original_id=3,
                title="Rejected",
                content="test",
                published_at=now,
                source_name="Source",
                source_tier=Tier.SUMMARIZE,
                first_seen_at=now,
                state=ArticleState.REJECTED,
                triaged_at=now,
            ),
            StoredArticle(
                url="https://example.com/pending",
                original_id=4,
                title="Pending",
                content="test",
                published_at=now,
                source_name="Source",
                source_tier=Tier.SUMMARIZE,
                first_seen_at=now,
                state=ArticleState.PENDING,
            ),
            StoredArticle(
                url="https://example.com/awaiting",
                original_id=5,
                title="Awaiting Triage",
                content="test",
                published_at=now,
                source_name="Source",
                source_tier=Tier.SUMMARIZE,
                first_seen_at=now,
                state=ArticleState.AWAITING_TRIAGE,
            ),
        ]

        today_path = store._partition_path(now)
        store._save_partition(today_path, articles)

        feedback = collect_triage_feedback(store, days=14, min_triaged=3)
        assert feedback.accepted_count == 2
        assert feedback.rejected_count == 1
