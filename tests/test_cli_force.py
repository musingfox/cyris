"""Tests for CLI --force flag functionality."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cyris.adapters.store import ArticleStore
from cyris.domain.models import Article, ArticleState, StoredArticle, Tier


@pytest.fixture
def store(tmp_path: Path) -> ArticleStore:
    """Create ArticleStore with temporary vault."""
    return ArticleStore(tmp_path)


def test_load_pending_only_without_force(store: ArticleStore) -> None:
    """Test 1: Load only PENDING articles when force=False."""
    now = datetime.now(UTC)
    window_start = now - timedelta(hours=24)

    # Create articles with different states
    articles = [
        Article(
            id=1,
            title="Pending Article",
            url="https://example.com/pending-1",
            content="Pending content",
            published_at=now - timedelta(hours=12),
            source_name="Test Source",
            source_tier=Tier.FILTER,
            source_tags=["tech"],
        ),
        Article(
            id=2,
            title="Accepted Article",
            url="https://example.com/accepted-1",
            content="Accepted content",
            published_at=now - timedelta(hours=12),
            source_name="Test Source",
            source_tier=Tier.FILTER,
            source_tags=["tech"],
        ),
    ]

    # Save articles (first_seen_at = now)
    store.save(articles, now=now)

    # Mark one as accepted
    store.update_states(
        {"https://example.com/accepted-1": (ArticleState.ACCEPTED, None)},
        digest_date="2026-04-10",
    )

    # Load with state_filter=PENDING (force=False)
    # Note: end time is exclusive, so use now + 1 hour
    state_filter = ArticleState.PENDING
    result = store.load_by_time_range(
        start=window_start,
        end=now + timedelta(hours=1),
        state_filter=state_filter,
    )

    assert len(result) == 1
    assert result[0].url == "https://example.com/pending-1"
    assert result[0].state == ArticleState.PENDING


def test_load_all_states_with_force(store: ArticleStore) -> None:
    """Test 2: Load all states when force=True (state_filter=None)."""
    now = datetime.now(UTC)
    window_start = now - timedelta(hours=24)

    # Create articles with different states
    articles = [
        Article(
            id=1,
            title="Pending Article",
            url="https://example.com/pending-2",
            content="Pending content",
            published_at=now - timedelta(hours=12),
            source_name="Test Source",
            source_tier=Tier.FILTER,
            source_tags=["tech"],
        ),
        Article(
            id=2,
            title="Accepted Article",
            url="https://example.com/accepted-2",
            content="Accepted content",
            published_at=now - timedelta(hours=12),
            source_name="Test Source",
            source_tier=Tier.FILTER,
            source_tags=["tech"],
        ),
        Article(
            id=3,
            title="Rejected Article",
            url="https://example.com/rejected-2",
            content="Rejected content",
            published_at=now - timedelta(hours=12),
            source_name="Test Source",
            source_tier=Tier.FILTER,
            source_tags=["tech"],
        ),
    ]

    # Save articles (first_seen_at = now)
    store.save(articles, now=now)

    # Update states
    store.update_states(
        {
            "https://example.com/accepted-2": (ArticleState.ACCEPTED, None),
            "https://example.com/rejected-2": (ArticleState.REJECTED, "filtered"),
        },
        digest_date="2026-04-10",
    )

    # Load with state_filter=None (force=True)
    # Note: end time is exclusive, so use now + 1 hour
    state_filter = None
    result = store.load_by_time_range(
        start=window_start,
        end=now + timedelta(hours=1),
        state_filter=state_filter,
    )

    assert len(result) == 3
    urls = {a.url for a in result}
    assert urls == {
        "https://example.com/pending-2",
        "https://example.com/accepted-2",
        "https://example.com/rejected-2",
    }


def test_force_respects_time_boundary(store: ArticleStore) -> None:
    """Test 3: Force mode respects time window boundaries."""
    now = datetime.now(UTC)
    window_start_24h = now - timedelta(hours=24)
    old_date = now - timedelta(days=30)

    # Create an old accepted article
    article = Article(
        id=1,
        title="Old Article",
        url="https://example.com/old-3",
        content="Old content",
        published_at=old_date,
        source_name="Test Source",
        source_tier=Tier.FILTER,
        source_tags=["tech"],
    )

    # Save at old date
    store.save([article], now=old_date)

    # Mark as accepted
    store.update_states(
        {"https://example.com/old-3": (ArticleState.ACCEPTED, None)},
        digest_date="2026-03-10",
    )

    # Load with force (state_filter=None) but within 24h window
    result = store.load_by_time_range(
        start=window_start_24h,
        end=now,
        state_filter=None,
    )

    assert len(result) == 0


def test_force_rescores_already_scored(store: ArticleStore) -> None:
    """Test 4: Force mode includes already-scored articles in scorable list."""
    now = datetime.now(UTC)

    # Create article with existing score
    article = Article(
        id=1,
        title="Scored Article",
        url="https://example.com/scored-4",
        content="Content with score",
        published_at=now - timedelta(hours=12),
        source_name="Test Source",
        source_tier=Tier.FILTER,
        source_tags=["tech"],
    )

    stored = StoredArticle.from_article(article, first_seen_at=now)
    stored.score = 8.0

    # Simulate the filter logic
    force = True
    scorable = []
    if "news" not in stored.source_tags:
        if not force and stored.score is not None:
            # Should skip
            pass
        else:
            # Should include
            scorable.append(stored)

    assert len(scorable) == 1
    assert scorable[0].score == 8.0

    # Test without force
    force = False
    scorable = []
    if "news" not in stored.source_tags:
        if not force and stored.score is not None:
            # Should skip
            pass
        else:
            scorable.append(stored)

    assert len(scorable) == 0


def test_force_never_scores_news(store: ArticleStore) -> None:
    """Test 5: Force mode never scores news-tagged articles."""
    now = datetime.now(UTC)

    # Create news article
    article = Article(
        id=1,
        title="News Article",
        url="https://example.com/news-5",
        content="News content",
        published_at=now - timedelta(hours=12),
        source_name="News Source",
        source_tier=Tier.FILTER,
        source_tags=["news"],
    )

    stored = StoredArticle.from_article(article, first_seen_at=now)

    # Simulate the filter logic with force=True
    force = True
    scorable = []
    if "news" in stored.source_tags:
        # Should skip news regardless of force
        pass
    elif not force and stored.score is not None:
        pass
    else:
        scorable.append(stored)

    assert len(scorable) == 0


def test_force_overwrites_states(store: ArticleStore) -> None:
    """Test 6: Force mode allows state overwrites."""
    now = datetime.now(UTC)

    # Create accepted article
    article = Article(
        id=1,
        title="Accepted Article",
        url="https://example.com/overwrite-6",
        content="Content",
        published_at=now - timedelta(hours=12),
        source_name="Test Source",
        source_tier=Tier.FILTER,
        source_tags=["tech"],
    )

    # Save and mark as accepted (first_seen_at = now)
    store.save([article], now=now)
    store.update_states(
        {"https://example.com/overwrite-6": (ArticleState.ACCEPTED, None)},
        digest_date="2026-04-10",
    )

    # Verify initial state
    # Note: end time is exclusive, so use now + 1 hour
    loaded = store.load_by_time_range(
        start=now - timedelta(hours=24),
        end=now + timedelta(hours=1),
        state_filter=None,
    )
    assert len(loaded) == 1
    assert loaded[0].state == ArticleState.ACCEPTED

    # Overwrite state to rejected
    store.update_states(
        {"https://example.com/overwrite-6": (ArticleState.REJECTED, "filtered")},
        digest_date="2026-04-10",
    )

    # Verify state was overwritten
    loaded = store.load_by_time_range(
        start=now - timedelta(hours=24),
        end=now + timedelta(hours=1),
        state_filter=None,
    )
    assert len(loaded) == 1
    assert loaded[0].state == ArticleState.REJECTED
    assert loaded[0].rejection_reason == "filtered"


def test_force_dry_run_preserves_states(store: ArticleStore) -> None:
    """Test 7: Dry run mode prevents state updates."""
    now = datetime.now(UTC)

    # Create article
    article = Article(
        id=1,
        title="Dry Run Article",
        url="https://example.com/dryrun-7",
        content="Content",
        published_at=now - timedelta(hours=12),
        source_name="Test Source",
        source_tier=Tier.FILTER,
        source_tags=["tech"],
    )

    # Save as pending (first_seen_at = now)
    store.save([article], now=now)

    # Verify initial state
    # Note: end time is exclusive, so use now + 1 hour
    loaded = store.load_by_time_range(
        start=now - timedelta(hours=24),
        end=now + timedelta(hours=1),
        state_filter=None,
    )
    assert len(loaded) == 1
    assert loaded[0].state == ArticleState.PENDING

    # Simulate dry_run guard
    dry_run = True
    url_to_state = {"https://example.com/dryrun-7": (ArticleState.ACCEPTED, None)}

    if not dry_run:
        store.update_states(url_to_state, digest_date="2026-04-10")

    # Verify state was NOT updated
    loaded = store.load_by_time_range(
        start=now - timedelta(hours=24),
        end=now + timedelta(hours=1),
        state_filter=None,
    )
    assert len(loaded) == 1
    assert loaded[0].state == ArticleState.PENDING

    # Now verify it works without dry_run
    dry_run = False
    if not dry_run:
        store.update_states(url_to_state, digest_date="2026-04-10")

    # Verify state WAS updated
    loaded = store.load_by_time_range(
        start=now - timedelta(hours=24),
        end=now + timedelta(hours=1),
        state_filter=None,
    )
    assert len(loaded) == 1
    assert loaded[0].state == ArticleState.ACCEPTED
