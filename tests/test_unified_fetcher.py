"""Tests for unified article fetcher."""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from cyris.domain.models import Article, Tier
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
        aliases={},
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
        aliases={},
    )

    # Newsletter version should win (last source wins)
    assert len(result) == 1
    assert result[0].source_name == "NL"


@pytest.mark.asyncio
async def test_passes_parameters_to_sources(rss_articles):
    """Test that fetch_all_articles passes all parameters to sources."""
    mock_source = AsyncMock()
    mock_source.fetch_articles.return_value = rss_articles

    test_cookies = {"session": "abc123", "auth": "xyz"}
    test_sources = {"test": "config"}
    test_aliases = {"alias": "source"}

    await fetch_all_articles(
        fetch_sources=[mock_source],
        after=datetime(2026, 3, 17),
        before=datetime(2026, 3, 19),
        sources=test_sources,
        aliases=test_aliases,
        limit=50,
        cookies=test_cookies,
    )

    # Verify all parameters were passed through
    mock_source.fetch_articles.assert_called_once_with(
        after=datetime(2026, 3, 17),
        before=datetime(2026, 3, 19),
        sources=test_sources,
        aliases=test_aliases,
        limit=50,
        cookies=test_cookies,
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
