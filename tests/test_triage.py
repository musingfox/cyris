"""Tests for triage module."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cyris.adapters.output.article_export import ArticleExporter
from cyris.adapters.store.article_store import ArticleStore
from cyris.domain.models import Article, ArticleState, FeedbackData, Tier
from cyris.service_layer.triage import export_accepted, process_feedback, scan_digests


@pytest.fixture
def store_with_articles(tmp_path: Path) -> ArticleStore:
    """Create a store with sample articles."""
    store = ArticleStore(tmp_path)
    now = datetime.now(UTC)
    articles = [
        Article(
            id=1,
            title="Article 1",
            url="https://example.com/1",
            content="Content 1",
            published_at=now,
            source_name="Source A",
            source_tier=Tier.FILTER,
        ),
        Article(
            id=2,
            title="Article 2",
            url="https://example.com/2",
            content="Content 2",
            published_at=now,
            source_name="Source B",
            source_tier=Tier.SUMMARIZE,
        ),
        Article(
            id=3,
            title="Article 3",
            url="https://example.com/3",
            content="Content 3",
            published_at=now,
            source_name="Source C",
            source_tier=Tier.FILTER,
        ),
    ]
    store.save(articles, now=now)
    return store


@pytest.fixture
def vault_with_digests(tmp_path: Path) -> Path:
    """Create a vault with 3 digest markdown files."""
    vault_path = tmp_path / "vault"
    digest_path = vault_path / "Digests"
    digest_path.mkdir(parents=True)

    # Create 3 digest files
    for i in range(1, 4):
        digest_file = digest_path / f"2026-03-{28 + i}_morning.md"
        content = f"""---
date: 2026-03-{28 + i}
---

# Digest

## Section

- **[Article {i}](https://example.com/{i})** (Source {chr(64 + i)})
  - [x] deep-read | [ ] track
"""
        digest_file.write_text(content)

    return vault_path


@pytest.fixture
def vault_with_no_digests(tmp_path: Path) -> Path:
    """Create an empty digest folder."""
    vault_path = tmp_path / "vault"
    digest_path = vault_path / "Digests"
    digest_path.mkdir(parents=True)
    return vault_path


# scan_digests tests


def test_scan_digests_returns_list(vault_with_digests: Path) -> None:
    """Test scan_digests with 3 digests returns 3 FeedbackData items."""
    result = scan_digests(vault_with_digests, "Digests", limit=5)

    assert len(result) == 3
    assert all(isinstance(item, FeedbackData) for item in result)


def test_scan_digests_no_digests_raises_error(vault_with_no_digests: Path) -> None:
    """Test scan_digests raises ValueError when no digest files found."""
    with pytest.raises(ValueError, match="No digest files found"):
        scan_digests(vault_with_no_digests, "Digests")


def test_scan_digests_nonexistent_vault_raises_error(tmp_path: Path) -> None:
    """Test scan_digests raises ValueError for nonexistent vault."""
    nonexistent = tmp_path / "nonexistent"
    with pytest.raises(ValueError, match="Digest folder does not exist"):
        scan_digests(nonexistent, "Digests")


def test_scan_digests_respects_limit(vault_with_digests: Path) -> None:
    """Test scan_digests respects limit parameter."""
    result = scan_digests(vault_with_digests, "Digests", limit=2)

    assert len(result) == 2


# process_feedback tests


def test_process_feedback_accepts_deep_read(store_with_articles: ArticleStore) -> None:
    """Test process_feedback accepts articles marked with deep_read."""
    feedback_list = [
        FeedbackData(
            digest_date="2026-03-29",
            digest_period="morning",
            articles=[
                {
                    "title": "Article 1",
                    "source": "Source A",
                    "url": "https://example.com/1",
                    "deep_read": True,
                    "track": False,
                },
                {
                    "title": "Article 2",
                    "source": "Source B",
                    "url": "https://example.com/2",
                    "deep_read": True,
                    "track": False,
                },
            ],
            liked_count=2,
        )
    ]

    result = process_feedback(feedback_list, store_with_articles)

    assert result.accepted_count == 2
    assert result.skipped_count == 0
    assert result.accepted_urls == ["https://example.com/1", "https://example.com/2"]


def test_process_feedback_ignores_unchecked(store_with_articles: ArticleStore) -> None:
    """Test process_feedback ignores unchecked articles."""
    feedback_list = [
        FeedbackData(
            digest_date="2026-03-29",
            digest_period="morning",
            articles=[
                {
                    "title": "Article 1",
                    "source": "Source A",
                    "url": "https://example.com/1",
                    "deep_read": True,
                    "track": False,
                },
                {
                    "title": "Article 2",
                    "source": "Source B",
                    "url": "https://example.com/2",
                    "deep_read": False,
                    "track": False,
                },
            ],
            liked_count=1,
        )
    ]

    result = process_feedback(feedback_list, store_with_articles)

    assert result.accepted_count == 1
    assert result.skipped_count == 0
    assert result.accepted_urls == ["https://example.com/1"]


def test_process_feedback_skips_unknown_urls(tmp_path: Path) -> None:
    """Test process_feedback skips URLs not in store."""
    empty_store = ArticleStore(tmp_path)

    feedback_list = [
        FeedbackData(
            digest_date="2026-03-29",
            digest_period="morning",
            articles=[
                {
                    "title": "Unknown Article",
                    "source": "Source X",
                    "url": "https://example.com/unknown",
                    "deep_read": True,
                    "track": False,
                }
            ],
            liked_count=1,
        )
    ]

    result = process_feedback(feedback_list, empty_store)

    assert result.accepted_count == 0
    assert result.skipped_count == 1
    assert result.accepted_urls == []


def test_process_feedback_skips_articles_without_url(store_with_articles: ArticleStore) -> None:
    """Test process_feedback skips articles with None URL."""
    feedback_list = [
        FeedbackData(
            digest_date="2026-03-29",
            digest_period="morning",
            articles=[
                {
                    "title": "Article without URL",
                    "source": "Source A",
                    "url": None,
                    "deep_read": True,
                    "track": False,
                }
            ],
            liked_count=1,
        )
    ]

    result = process_feedback(feedback_list, store_with_articles)

    assert result.accepted_count == 0
    assert result.skipped_count == 1


# export_accepted tests


def test_export_accepted_with_articles(tmp_path: Path) -> None:
    """Test export_accepted exports only the specified URLs."""
    agent_vault = tmp_path / "agent-vault"
    store = ArticleStore(agent_vault)

    now = datetime.now(UTC)
    articles = [
        Article(
            id=i,
            title=f"Article {i}",
            url=f"https://example.com/{i}",
            content=f"Content {i}",
            published_at=now,
            source_name=f"Source {i}",
            source_tier=Tier.FILTER,
        )
        for i in range(1, 6)
    ]
    store.save(articles, now=now)

    # Accept all articles
    for article in articles:
        store.update_article_state(article.url, ArticleState.ACCEPTED)

    # Export only 2 of the 5 accepted
    user_vault = tmp_path / "user-vault"
    user_vault.mkdir()
    exporter = ArticleExporter()
    accepted_urls = ["https://example.com/1", "https://example.com/3"]

    paths = export_accepted(store, exporter, user_vault, accepted_urls, folder="Reading")

    assert len(paths) == 2
    assert all(p.exists() for p in paths)
    assert all(p.parent.name == "Reading" for p in paths)


def test_export_accepted_with_no_urls(tmp_path: Path) -> None:
    """Test export_accepted returns empty list when no URLs provided."""
    store = ArticleStore(tmp_path / "agent-vault")

    user_vault = tmp_path / "user-vault"
    user_vault.mkdir()
    exporter = ArticleExporter()

    paths = export_accepted(store, exporter, user_vault, accepted_urls=[], folder="Reading")

    assert paths == []
