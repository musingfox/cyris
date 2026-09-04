"""Integration tests for ArticleStore with pipeline and CLI flow."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cyris.adapters.store import ArticleStore
from cyris.domain.models import Article, ArticleState, Tier


@pytest.fixture
def vault_path(tmp_path: Path) -> Path:
    """Create temporary vault path."""
    return tmp_path


@pytest.fixture
def store(vault_path: Path) -> ArticleStore:
    """Create ArticleStore with temporary vault."""
    return ArticleStore(vault_path)


def test_end_to_end_flow(store: ArticleStore) -> None:
    """Test complete flow: save → process → update states."""
    now = datetime.now(UTC)

    # Step 1: Fetch articles (simulated)
    articles = [
        Article(
            id=101,
            title="Filter Article 1",
            url="https://example.com/f1",
            content="Content 1",
            published_at=now,
            source_name="Source A",
            source_tier=Tier.FILTER,
        ),
        Article(
            id=102,
            title="Filter Article 2",
            url="https://example.com/f2",
            content="Content 2",
            published_at=now,
            source_name="Source B",
            source_tier=Tier.FILTER,
        ),
        Article(
            id=103,
            title="Summarize Article",
            url="https://example.com/s1",
            content="Content 3",
            published_at=now,
            source_name="Source C",
            source_tier=Tier.SUMMARIZE,
        ),
    ]

    # Step 2: Save articles
    save_result = store.save(articles, now=now)
    assert save_result.saved_count == 3

    # Step 3: Simulate processing (filter tier: 1 accepted, 1 rejected; summarize: all accepted)
    accepted_urls = ["https://example.com/f1", "https://example.com/s1"]
    rejected_urls = ["https://example.com/f2"]

    url_to_state = {}
    for url in accepted_urls:
        url_to_state[url] = (ArticleState.ACCEPTED, None)
    for url in rejected_urls:
        url_to_state[url] = (ArticleState.REJECTED, "filtered")

    # Step 4: Update states
    updated = store.update_states(url_to_state, digest_date="2026-03-30")
    assert updated == 3

    # Step 5: Verify states
    end_time = now + timedelta(hours=1)
    all_articles = store.load_by_time_range(now, end_time)
    assert len(all_articles) == 3

    accepted = store.load_by_time_range(now, end_time, state_filter=ArticleState.ACCEPTED)
    assert len(accepted) == 2
    assert {a.url for a in accepted} == {"https://example.com/f1", "https://example.com/s1"}

    rejected = store.load_by_time_range(now, end_time, state_filter=ArticleState.REJECTED)
    assert len(rejected) == 1
    assert rejected[0].url == "https://example.com/f2"
    assert rejected[0].rejection_reason == "filtered"


def test_deduplication_across_runs(store: ArticleStore) -> None:
    """Test that duplicate articles are skipped across multiple digest runs."""
    now = datetime.now(UTC)

    article = Article(
        id=101,
        title="Article",
        url="https://example.com/article",
        content="Content",
        published_at=now,
        source_name="Source",
        source_tier=Tier.FILTER,
    )

    # First run
    result1 = store.save([article], now=now)
    assert result1.saved_count == 1

    # Second run (same article)
    result2 = store.save([article], now=now)
    assert result2.saved_count == 0
    assert result2.skipped_count == 1

    # Verify only one copy exists
    end_time = now + timedelta(hours=1)
    all_articles = store.load_by_time_range(now, end_time)
    assert len(all_articles) == 1


def test_store_reload_preserves_ref_urls(vault_path: Path) -> None:
    now = datetime.now(UTC)
    article = Article(
        id="newsletter-abc",
        title="Newsletter Article",
        url="newsletter:abc",
        content="Content",
        published_at=now,
        source_name="Newsletter",
        source_tier=Tier.SUMMARIZE,
        ref_urls=["https://example.com/a"],
    )
    ArticleStore(vault_path).save([article], now=now)

    reloaded = ArticleStore(vault_path).get_by_urls([article.url])

    assert len(reloaded) == 1
    assert reloaded[0].ref_urls == ["https://example.com/a"]


def test_dedup_window_matches_rss_worker_retention() -> None:
    """The store's dedup scan and the Worker's prune must span the same days.

    Shortening the scan re-ingests what the buffer still holds; shortening the
    prune drops articles the scan expects to recognise. Neither side errors.
    """
    import re
    from pathlib import Path

    from cyris.adapters.store.article_store import _DEDUP_SCAN_DAYS

    index_js = Path(__file__).resolve().parents[1] / "workers" / "rss" / "src" / "index.js"
    match = re.search(r"const RETENTION_DAYS = (\d+)", index_js.read_text())
    assert match, "RETENTION_DAYS not found in workers/rss/src/index.js"
    assert int(match.group(1)) == _DEDUP_SCAN_DAYS
