"""Tests for unified article fetcher."""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from cyris.domain.models import NEWSLETTER_SOURCE_TYPE, Article, Tier
from cyris.service_layer.fetching import fetch_all_articles


@pytest.fixture
def rss_articles():
    return [
        Article(
            id=1,
            title="RSS 1",
            url="https://a.com/1",
            content="C",
            published_at=datetime(2026, 3, 18),
            source_name="RSS",
            source_tier=Tier.FILTER,
        ),
        Article(
            id=2,
            title="RSS 2",
            url="https://a.com/2",
            content="C",
            published_at=datetime(2026, 3, 18),
            source_name="RSS",
            source_tier=Tier.FILTER,
        ),
    ]


@pytest.fixture
def newsletter_articles():
    return [
        Article(
            id="nl-1",
            title="NL 1",
            url="https://b.com/1",
            content="C",
            published_at=datetime(2026, 3, 18, 8, 0, 0),
            source_name="NL",
            source_tier=Tier.SUMMARIZE,
        ),
    ]


@pytest.mark.asyncio
async def test_combines_sources(rss_articles, newsletter_articles):
    """Test that fetch_all_articles combines multiple sources."""
    mock_rss_source = AsyncMock()
    mock_rss_source.fetch_articles.return_value = rss_articles

    mock_newsletter_source = AsyncMock()
    mock_newsletter_source.fetch_articles.return_value = newsletter_articles

    result, failed = await fetch_all_articles(
        fetch_sources=[mock_rss_source, mock_newsletter_source],
        after=datetime(2026, 3, 17),
        before=datetime(2026, 3, 19),
        sources={},
    )

    assert len(result) == 3
    assert failed == []
    mock_rss_source.fetch_articles.assert_called_once()
    mock_newsletter_source.fetch_articles.assert_called_once()


@pytest.mark.asyncio
async def test_deduplicates_by_url(newsletter_articles):
    """Test that fetch_all_articles deduplicates by URL (last source wins)."""
    rss_duplicate = [
        Article(
            id=1,
            title="RSS version",
            url="https://b.com/1",
            content="RSS",
            published_at=datetime(2026, 3, 18),
            source_name="RSS",
            source_tier=Tier.FILTER,
        )
    ]

    mock_rss_source = AsyncMock()
    mock_rss_source.fetch_articles.return_value = rss_duplicate

    mock_newsletter_source = AsyncMock()
    mock_newsletter_source.fetch_articles.return_value = newsletter_articles

    result, _ = await fetch_all_articles(
        fetch_sources=[mock_rss_source, mock_newsletter_source],
        after=datetime(2026, 3, 17),
        before=datetime(2026, 3, 19),
        sources={},
    )

    # Newsletter version should win (last source wins)
    assert len(result) == 1
    assert result[0].source_name == "NL"


def _issue(article_id: str, title: str, url: str = "https://s.com/account/settings/email"):
    return Article(
        id=article_id,
        title=title,
        url=url,
        content="C",
        published_at=datetime(2026, 9, 1),
        source_name="NL",
        source_tier=Tier.SUMMARIZE,
        source_type=NEWSLETTER_SOURCE_TYPE,
    )


async def _fetch(*batches: list[Article]) -> list[Article]:
    fetch_sources = []
    for batch in batches:
        source = AsyncMock()
        source.fetch_articles.return_value = batch
        fetch_sources.append(source)
    result, _ = await fetch_all_articles(
        fetch_sources=fetch_sources,
        after=datetime(2026, 8, 31),
        before=datetime(2026, 9, 2),
        sources={},
    )
    return result


async def test_sibling_newsletter_issues_sharing_a_link_both_survive():
    result = await _fetch([_issue("a", "Issue 1"), _issue("b", "Issue 2")])
    assert {a.id for a in result} == {"a", "b"}


async def test_rss_then_newsletter_with_the_same_url_still_collapses():
    rss = Article(
        id=1,
        title="RSS version",
        url="https://b.com/1",
        content="RSS",
        published_at=datetime(2026, 3, 18),
        source_name="RSS",
        source_tier=Tier.FILTER,
    )
    result = await _fetch([rss], [_issue("nl", "NL 1", url="https://b.com/1")])
    assert len(result) == 1
    assert result[0].source_name == "NL"


async def test_the_same_newsletter_issue_twice_collapses():
    result = await _fetch([_issue("a", "Issue 1"), _issue("a", "Issue 1")])
    assert len(result) == 1


async def test_a_redelivered_sibling_is_not_kept_twice():
    result = await _fetch([_issue("a", "Issue 1"), _issue("b", "Issue 2"), _issue("b", "Issue 2")])
    assert sorted(a.id for a in result) == ["a", "b"]


@pytest.mark.asyncio
async def test_passes_parameters_to_sources(rss_articles):
    """Test that fetch_all_articles passes all parameters to sources."""
    mock_source = AsyncMock()
    mock_source.fetch_articles.return_value = rss_articles

    test_sources = {"test": "config"}

    await fetch_all_articles(
        fetch_sources=[mock_source],
        after=datetime(2026, 3, 17),
        before=datetime(2026, 3, 19),
        sources=test_sources,
        limit=50,
    )

    # Verify all parameters were passed through
    mock_source.fetch_articles.assert_called_once_with(
        after=datetime(2026, 3, 17),
        before=datetime(2026, 3, 19),
        sources=test_sources,
        limit=50,
    )


@pytest.mark.asyncio
async def test_handles_empty_sources():
    """Test that fetch_all_articles handles empty source list."""
    result, failed = await fetch_all_articles(
        fetch_sources=[],
        after=datetime(2026, 3, 17),
        before=datetime(2026, 3, 19),
        sources={},
    )

    assert result == []
    assert failed == []


@pytest.mark.asyncio
async def test_handles_source_failure(rss_articles):
    """Test that fetch_all_articles continues when one source fails."""
    mock_bad_source = AsyncMock()
    mock_bad_source.fetch_articles.side_effect = Exception("Source down")

    mock_good_source = AsyncMock()
    mock_good_source.fetch_articles.return_value = rss_articles

    result, failed = await fetch_all_articles(
        fetch_sources=[mock_bad_source, mock_good_source],
        after=datetime(2026, 3, 17),
        before=datetime(2026, 3, 19),
        sources={},
    )

    assert len(result) == 2
    assert failed == ["AsyncMock"]
    mock_bad_source.fetch_articles.assert_called_once()
    mock_good_source.fetch_articles.assert_called_once()
