"""The article store contract, run against both backends.

Every case here runs twice: once against the local JSON partitions and once
against D1 (real SQL, stdlib sqlite3 — see `SqliteD1`). That is what makes the
D1 store a drop-in rather than a lookalike.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fakes import SqliteD1

from cyris.adapters.store import ArticleStore
from cyris.adapters.store.d1_store import D1ArticleStore
from cyris.domain.models import Article, ArticleState, StoredArticle, Tier


@pytest.fixture(params=["json", "d1"])
def store(request, tmp_path: Path):
    """The store under test, one instance per backend."""
    if request.param == "json":
        return ArticleStore(tmp_path)
    return D1ArticleStore(SqliteD1())


@pytest.fixture
def sample_articles() -> list[Article]:
    """Generate sample articles for testing."""
    now = datetime.now(UTC)
    return [
        Article(
            id=101,
            title="Article 1",
            url="https://example.com/1",
            content="Content 1",
            author="Author 1",
            published_at=now,
            source_name="Source A",
            source_tier=Tier.FILTER,
            source_tags=["tech"],
        ),
        Article(
            id=102,
            title="Article 2",
            url="https://example.com/2",
            content="Content 2",
            author="Author 2",
            published_at=now,
            source_name="Source B",
            source_tier=Tier.SUMMARIZE,
            source_tags=["news"],
        ),
        Article(
            id=103,
            title="Article 3",
            url="https://example.com/3",
            content="Content 3",
            author=None,
            published_at=now,
            source_name="Source C",
            source_tier=Tier.FILTER,
            source_tags=[],
        ),
    ]


def test_save_new_articles(store: ArticleStore, sample_articles: list[Article]) -> None:
    """Contract 1: Save 3 new articles."""
    now = datetime.now(UTC)
    result = store.save(sample_articles, now=now)

    assert result.saved_count == 3
    assert result.skipped_count == 0


def test_save_with_duplicates(store: ArticleStore, sample_articles: list[Article]) -> None:
    """Contract 1: Save 2 articles where 1 URL already in store."""
    now = datetime.now(UTC)

    # Save first batch
    store.save([sample_articles[0]], now=now)

    # Save second batch with one duplicate
    result = store.save([sample_articles[0], sample_articles[1]], now=now)

    assert result.saved_count == 1
    assert result.skipped_count == 1


def test_save_mixed_ids(store: ArticleStore) -> None:
    """Feed ids are ints, newsletter ids are strings; both must round-trip."""
    now = datetime.now(UTC)
    articles = [
        Article(
            id=101,
            title="Feed Article",
            url="https://example.com/m1",
            content="Content",
            published_at=now,
            source_name="RSS",
            source_tier=Tier.FILTER,
        ),
        Article(
            id="newsletter-123",
            title="Newsletter Article",
            url="https://example.com/n1",
            content="Content",
            published_at=now,
            source_name="Newsletter",
            source_tier=Tier.SUMMARIZE,
        ),
        Article(
            id=102,
            title="Feed Article 2",
            url="https://example.com/m2",
            content="Content",
            published_at=now,
            source_name="RSS",
            source_tier=Tier.FILTER,
        ),
    ]

    result = store.save(articles, now=now)

    assert result.saved_count == 3
    stored = store.get_by_urls([a.url for a in articles])
    assert {a.original_id for a in stored} == {101, "newsletter-123", 102}


def test_update_states_existing_articles(
    store: ArticleStore, sample_articles: list[Article]
) -> None:
    """Contract 2: Update 5 URLs (3 exist, 2 don't)."""
    now = datetime.now(UTC)
    store.save(sample_articles, now=now)

    url_to_state = {
        "https://example.com/1": (ArticleState.ACCEPTED, None),
        "https://example.com/2": (ArticleState.ACCEPTED, None),
        "https://example.com/3": (ArticleState.REJECTED, "noise"),
        "https://example.com/nonexistent1": (ArticleState.ACCEPTED, None),
        "https://example.com/nonexistent2": (ArticleState.ACCEPTED, None),
    }

    updated = store.update_states(url_to_state, digest_date="2026-03-30")

    assert updated == 3  # Only existing URLs


def test_update_states_with_rejection_reason(
    store: ArticleStore, sample_articles: list[Article]
) -> None:
    """Contract 2: Update with state=REJECTED + rejection_reason."""
    now = datetime.now(UTC)
    store.save([sample_articles[0]], now=now)

    url_to_state = {
        "https://example.com/1": (ArticleState.REJECTED, "noise"),
    }

    store.update_states(url_to_state, digest_date="2026-03-30")

    # Load and verify
    articles = store.load_by_time_range(now - timedelta(hours=1), now + timedelta(hours=1))
    assert len(articles) == 1
    assert articles[0].state == ArticleState.REJECTED
    assert articles[0].rejection_reason == "noise"
    assert articles[0].digest_date == "2026-03-30"


def test_update_states_overwrites_existing(
    store: ArticleStore, sample_articles: list[Article]
) -> None:
    """Contract 2: Update existing article state (pending→accepted)."""
    now = datetime.now(UTC)
    store.save([sample_articles[0]], now=now)

    # First update
    store.update_states(
        {"https://example.com/1": (ArticleState.ACCEPTED, None)},
        digest_date="2026-03-30",
    )

    # Second update (should overwrite)
    store.update_states(
        {"https://example.com/1": (ArticleState.REJECTED, "changed mind")},
        digest_date="2026-03-31",
    )

    articles = store.load_by_time_range(now - timedelta(hours=1), now + timedelta(hours=1))
    assert len(articles) == 1
    assert articles[0].state == ArticleState.REJECTED
    assert articles[0].rejection_reason == "changed mind"
    assert articles[0].digest_date == "2026-03-31"


def test_load_by_time_range_single_day(store: ArticleStore, sample_articles: list[Article]) -> None:
    """Contract 3: Load single day."""
    now = datetime.now(UTC)
    store.save(sample_articles, now=now)

    articles = store.load_by_time_range(
        now - timedelta(hours=1),
        now + timedelta(hours=1),
    )

    assert len(articles) == 3
    assert {a.url for a in articles} == {
        "https://example.com/1",
        "https://example.com/2",
        "https://example.com/3",
    }


def test_load_by_time_range_multiple_days(store: ArticleStore) -> None:
    """Contract 3: Load across multiple days."""
    day1 = datetime(2026, 3, 28, 12, 0, 0, tzinfo=UTC)
    day2 = datetime(2026, 3, 29, 12, 0, 0, tzinfo=UTC)
    day3 = datetime(2026, 3, 30, 12, 0, 0, tzinfo=UTC)

    # Save articles on different days
    for day, article_id in [(day1, 101), (day2, 102), (day3, 103)]:
        article = Article(
            id=article_id,
            title=f"Article {article_id}",
            url=f"https://example.com/{article_id}",
            content="Content",
            published_at=day,
            source_name="Source",
            source_tier=Tier.FILTER,
        )
        store.save([article], now=day)

    # Load all 3 days
    articles = store.load_by_time_range(
        day1 - timedelta(hours=1),
        day3 + timedelta(hours=1),
    )

    assert len(articles) == 3
    assert [a.original_id for a in articles] == [101, 102, 103]  # Sorted by first_seen_at


def test_load_by_time_range_with_state_filter(
    store: ArticleStore, sample_articles: list[Article]
) -> None:
    """Contract 3: state_filter=PENDING → only pending articles."""
    now = datetime.now(UTC)
    store.save(sample_articles, now=now)

    # Update one to accepted
    store.update_states(
        {"https://example.com/1": (ArticleState.ACCEPTED, None)},
        digest_date="2026-03-30",
    )

    # Load only pending
    pending = store.load_by_time_range(
        now - timedelta(hours=1),
        now + timedelta(hours=1),
        state_filter=ArticleState.PENDING,
    )

    assert len(pending) == 2
    assert all(a.state == ArticleState.PENDING for a in pending)


def test_load_by_time_range_empty(store: ArticleStore) -> None:
    """Contract 3: Empty range (no files) → returns []."""
    now = datetime.now(UTC)
    articles = store.load_by_time_range(now - timedelta(hours=1), now + timedelta(hours=1))

    assert articles == []


def test_stored_article_from_article() -> None:
    """Contract 4: StoredArticle.from_article."""
    now = datetime.now(UTC)
    article = Article(
        id=101,
        title="Test Article",
        url="https://example.com/test",
        content="Test content",
        author="Test Author",
        published_at=now,
        source_name="Test Source",
        source_tier=Tier.FILTER,
        source_tags=["test"],
    )

    stored = StoredArticle.from_article(article, first_seen_at=now)

    assert stored.url == article.url
    assert stored.original_id == article.id
    assert stored.title == article.title
    assert stored.content == article.content
    assert stored.author == article.author
    assert stored.published_at == article.published_at
    assert stored.source_name == article.source_name
    assert stored.source_tier == article.source_tier
    assert stored.source_tags == article.source_tags
    assert stored.state == ArticleState.PENDING
    assert stored.first_seen_at == now
    assert stored.digest_date is None
    assert stored.rejection_reason is None


def test_stored_article_to_article() -> None:
    """Contract 5: StoredArticle.to_article."""
    now = datetime.now(UTC)
    stored = StoredArticle(
        url="https://example.com/test",
        original_id=101,
        title="Test Article",
        content="Test content",
        author="Test Author",
        published_at=now,
        source_name="Test Source",
        source_tier=Tier.FILTER,
        source_tags=["test"],
        state=ArticleState.ACCEPTED,
        first_seen_at=now,
        digest_date="2026-03-30",
        rejection_reason=None,
    )

    article = stored.to_article()

    assert article.id == stored.original_id
    assert article.title == stored.title
    assert article.url == stored.url
    assert article.content == stored.content
    assert article.author == stored.author
    assert article.published_at == stored.published_at
    assert article.source_name == stored.source_name
    assert article.source_tier == stored.source_tier
    assert article.source_tags == stored.source_tags


def test_update_states_preserves_human_votes(
    store: ArticleStore, sample_articles: list[Article]
) -> None:
    """A triaged_at stamp marks a human vote, which a re-run must not overwrite."""
    now = datetime.now(UTC)
    store.save([sample_articles[0]], now=now)
    store.reject(["https://example.com/1"], reason="manual_triage")
    store.update_triage_timestamp(["https://example.com/1"], now)

    updated = store.update_states(
        {"https://example.com/1": (ArticleState.ACCEPTED, None)},
        digest_date="2026-03-30",
    )

    assert updated == 0
    [article] = store.get_by_urls(["https://example.com/1"])
    assert article.state == ArticleState.REJECTED


def test_reset_to_pending_clears_triage_stamp(
    store: ArticleStore, sample_articles: list[Article]
) -> None:
    """Undo must drop triaged_at, or the update_states guard strands the row as PENDING."""
    now = datetime.now(UTC)
    store.save([sample_articles[0]], now=now)
    store.reject(["https://example.com/1"], reason="manual_triage")
    store.update_triage_timestamp(["https://example.com/1"], now)

    assert store.reset_to_pending("https://example.com/1")

    [article] = store.get_by_urls(["https://example.com/1"])
    assert article.triaged_at is None
    assert (
        store.update_states(
            {"https://example.com/1": (ArticleState.ACCEPTED, None)},
            digest_date="2026-03-30",
        )
        == 1
    )


def test_update_states_invalid_date_format(store: ArticleStore) -> None:
    """update_states should raise ValueError for bad digest_date format."""
    with pytest.raises(ValueError, match="Invalid digest_date format"):
        store.update_states({}, digest_date="2026-13-99")


# --- Contract 1: list_articles ---


def test_list_articles_filter_by_state(store: ArticleStore, sample_articles: list[Article]) -> None:
    """list_articles with state=PENDING returns only pending."""
    now = datetime.now(UTC)
    store.save(sample_articles, now=now)
    store.update_states({"https://example.com/1": (ArticleState.ACCEPTED, None)}, "2026-03-30")
    result = store.list_articles(state=ArticleState.PENDING)
    assert len(result) == 2
    assert all(a.state == ArticleState.PENDING for a in result)


def test_list_articles_multiple_states(store: ArticleStore, sample_articles: list[Article]) -> None:
    """list_articles with multiple states."""
    now = datetime.now(UTC)
    store.save(sample_articles, now=now)
    store.update_states({"https://example.com/1": (ArticleState.ACCEPTED, None)}, "2026-03-30")
    result = store.list_articles(state=[ArticleState.PENDING, ArticleState.ACCEPTED])
    assert len(result) == 3


def test_list_articles_limit(store: ArticleStore, sample_articles: list[Article]) -> None:
    """list_articles with limit."""
    now = datetime.now(UTC)
    store.save(sample_articles, now=now)
    result = store.list_articles(limit=2)
    assert len(result) == 2


def test_list_articles_offset(store: ArticleStore, sample_articles: list[Article]) -> None:
    """list_articles with offset."""
    now = datetime.now(UTC)
    store.save(sample_articles, now=now)
    result = store.list_articles(offset=1, limit=1)
    assert len(result) == 1


def test_list_articles_sort_by_title(store: ArticleStore, sample_articles: list[Article]) -> None:
    """list_articles sorted by title ascending."""
    now = datetime.now(UTC)
    store.save(sample_articles, now=now)
    result = store.list_articles(sort_by="title", descending=False)
    titles = [a.title for a in result]
    assert titles == sorted(titles)


def test_list_articles_invalid_sort(store: ArticleStore) -> None:
    """list_articles with invalid sort_by raises ValueError."""
    with pytest.raises(ValueError, match="Invalid sort_by"):
        store.list_articles(sort_by="nonexistent")


# --- Contract 2: update_article_state ---


def test_update_article_state_accept(store: ArticleStore, sample_articles: list[Article]) -> None:
    """update_article_state pending→accepted."""
    now = datetime.now(UTC)
    store.save(sample_articles, now=now)
    success = store.update_article_state("https://example.com/1", ArticleState.ACCEPTED)
    assert success is True
    result = store.list_articles(state=ArticleState.ACCEPTED)
    assert len(result) == 1


def test_update_article_state_reject_with_reason(
    store: ArticleStore, sample_articles: list[Article]
) -> None:
    """update_article_state with rejection reason."""
    now = datetime.now(UTC)
    store.save(sample_articles, now=now)
    success = store.update_article_state(
        "https://example.com/1", ArticleState.REJECTED, reason="spam"
    )
    assert success is True
    result = store.list_articles(state=ArticleState.REJECTED)
    assert len(result) == 1
    assert result[0].rejection_reason == "spam"


def test_update_article_state_not_found(store: ArticleStore) -> None:
    """update_article_state for non-existent URL returns False."""
    success = store.update_article_state("https://nonexistent.com", ArticleState.ACCEPTED)
    assert success is False


def test_accept_reject_reset_semantics(store: ArticleStore, sample_articles: list[Article]) -> None:
    """accept/reject/reset_to_pending drive the article lifecycle."""
    from cyris.domain.triage import RejectReason

    now = datetime.now(UTC)
    store.save(sample_articles, now=now)

    assert store.accept(["https://example.com/1"]) == 1
    assert store.reject(["https://example.com/2"], reason=RejectReason.MANUAL_TRIAGE) == 1

    rejected = store.list_articles(state=ArticleState.REJECTED)
    assert rejected[0].rejection_reason == "manual_triage"

    assert store.reset_to_pending("https://example.com/1") is True
    accepted = store.list_articles(state=ArticleState.ACCEPTED)
    assert accepted == []

    # Unknown URLs count as zero updates
    assert store.accept(["https://nonexistent.com"]) == 0
    assert store.reject(["https://nonexistent.com"], reason="x") == 0
    assert store.reset_to_pending("https://nonexistent.com") is False


# --- Contract 3: delete_articles ---


def test_delete_articles_by_state(store: ArticleStore, sample_articles: list[Article]) -> None:
    """delete_articles deletes all articles with given state."""
    now = datetime.now(UTC)
    store.save(sample_articles, now=now)
    store.update_states(
        {
            "https://example.com/1": (ArticleState.REJECTED, "spam"),
            "https://example.com/2": (ArticleState.REJECTED, "noise"),
        },
        "2026-03-30",
    )
    deleted = store.delete_articles(state=ArticleState.REJECTED)
    assert deleted == 2
    remaining = store.list_articles()
    assert len(remaining) == 1


def test_delete_articles_older_than(store: ArticleStore) -> None:
    """delete_articles with older_than_days filter."""
    now = datetime.now(UTC)
    # Old article: within update_states scan range (7 days) but older than delete threshold (5 days)
    old_date = now - timedelta(days=6)
    new_date = now - timedelta(days=2)
    old_article = Article(
        id=1,
        title="Old",
        url="https://example.com/old",
        content="C",
        published_at=old_date,
        source_name="S",
        source_tier=Tier.FILTER,
    )
    new_article = Article(
        id=2,
        title="New",
        url="https://example.com/new",
        content="C",
        published_at=new_date,
        source_name="S",
        source_tier=Tier.FILTER,
    )
    store.save([old_article], now=old_date)
    store.save([new_article], now=new_date)

    # Use today as digest_date to ensure both articles are in scan range
    today_str = now.strftime("%Y-%m-%d")
    store.update_states(
        {
            "https://example.com/old": (ArticleState.REJECTED, "x"),
            "https://example.com/new": (ArticleState.REJECTED, "y"),
        },
        today_str,
    )
    # Delete articles older than 5 days (should delete old but not new)
    deleted = store.delete_articles(state=ArticleState.REJECTED, older_than_days=5)
    assert deleted == 1


def test_delete_articles_preserves_triaged_rows(store: ArticleStore) -> None:
    old = datetime.now(UTC) - timedelta(days=60)
    triaged = StoredArticle.from_article(
        Article(
            id=1,
            title="Triaged",
            url="https://example.com/triaged",
            content="C",
            published_at=old,
            source_name="S",
            source_tier=Tier.FILTER,
        ),
        first_seen_at=old,
    )
    triaged.state = ArticleState.REJECTED
    triaged.triaged_at = old
    untriaged = StoredArticle.from_article(
        Article(
            id=2,
            title="Untriaged",
            url="https://example.com/untriaged",
            content="C",
            published_at=old,
            source_name="S",
            source_tier=Tier.FILTER,
        ),
        first_seen_at=old,
    )
    untriaged.state = ArticleState.REJECTED

    if isinstance(store, D1ArticleStore):
        store.import_articles([triaged, untriaged])
    else:
        store._save_partition(store._partition_path(old), [triaged, untriaged])

    assert store.delete_articles(ArticleState.REJECTED, 30) == 1
    assert [article.url for article in store.list_articles()] == [
        "https://example.com/triaged"
    ]


def test_delete_articles_negative_days(store: ArticleStore) -> None:
    """delete_articles with negative older_than_days raises ValueError."""
    with pytest.raises(ValueError, match="older_than_days"):
        store.delete_articles(state=ArticleState.REJECTED, older_than_days=-1)


# --- Contract 4: count_by_state ---


def test_count_by_state_empty(store: ArticleStore) -> None:
    """count_by_state on empty store returns empty dict."""
    counts = store.count_by_state()
    assert counts == {}


def test_count_by_state_mixed_states(store: ArticleStore, sample_articles: list[Article]) -> None:
    """count_by_state returns correct counts for mixed states."""
    now = datetime.now(UTC)
    store.save(sample_articles, now=now)

    # Update states: 1 accepted, 1 rejected, 1 pending
    store.update_states(
        {
            "https://example.com/1": (ArticleState.ACCEPTED, None),
            "https://example.com/2": (ArticleState.REJECTED, "noise"),
        },
        digest_date="2026-03-30",
    )

    counts = store.count_by_state()
    assert counts == {
        ArticleState.PENDING: 1,
        ArticleState.ACCEPTED: 1,
        ArticleState.REJECTED: 1,
    }


def test_update_triage_timestamp(store: ArticleStore, sample_articles: list[Article]) -> None:
    """update_triage_timestamp updates triaged_at for matching articles."""
    now = datetime.now(UTC)
    store.save(sample_articles, now=now)

    triaged_at = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
    urls = ["https://example.com/1", "https://example.com/2"]

    updated = store.update_triage_timestamp(urls, triaged_at)

    assert updated == 2

    # Verify timestamps were updated
    articles = store.get_by_urls(urls)
    for article in articles:
        assert article.triaged_at == triaged_at
