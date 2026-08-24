"""Tests for MinifluxSource adapter."""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from cyris.adapters.fetch.miniflux_source import MinifluxSource
from cyris.domain.models import Article, Tier


@pytest.mark.asyncio
async def test_fetch_articles_delegates_to_client():
    """Test that fetch_articles delegates to MinifluxClient.fetch_entries."""
    mock_client = AsyncMock()
    expected_articles = [
        Article(
            id=1,
            title="Test",
            url="https://example.com",
            content="Content",
            published_at=datetime(2026, 3, 18),
            source_name="Test",
            source_tier=Tier.FILTER,
        )
    ]
    mock_client.fetch_entries.return_value = expected_articles

    source = MinifluxSource(mock_client)
    result = await source.fetch_articles(
        after=datetime(2026, 3, 17),
        before=datetime(2026, 3, 19),
        sources={},
        aliases={"alias": "source"},
        limit=100,
    )

    assert result == expected_articles
    mock_client.fetch_entries.assert_called_once_with(
        after=datetime(2026, 3, 17),
        before=datetime(2026, 3, 19),
        sources={},
        aliases={"alias": "source"},
        limit=100,
    )


@pytest.mark.asyncio
async def test_mark_as_read_filters_int_ids():
    """Test that mark_as_read filters to int IDs only."""
    mock_client = AsyncMock()
    source = MinifluxSource(mock_client)

    await source.mark_as_read([1, 2, "3", "not-an-int", "456"])

    # Should convert string digits to int, filter out non-numeric strings
    mock_client.mark_as_read.assert_called_once_with([1, 2, 3, 456])


@pytest.mark.asyncio
async def test_mark_as_read_empty_list():
    """Test that mark_as_read handles empty list."""
    mock_client = AsyncMock()
    source = MinifluxSource(mock_client)

    await source.mark_as_read([])

    # Should not call client if no IDs
    mock_client.mark_as_read.assert_not_called()


@pytest.mark.asyncio
async def test_mark_as_read_all_non_numeric():
    """Test that mark_as_read handles all non-numeric IDs."""
    mock_client = AsyncMock()
    source = MinifluxSource(mock_client)

    await source.mark_as_read(["abc", "xyz", "not-a-number"])

    # Should not call client if no valid int IDs
    mock_client.mark_as_read.assert_not_called()


@pytest.mark.asyncio
async def test_health_check_success():
    """Test that health_check returns True when connection succeeds."""
    mock_client = AsyncMock()
    mock_client.fetch_entries.return_value = []

    source = MinifluxSource(mock_client)
    result = await source.health_check()

    assert result is True
    mock_client.fetch_entries.assert_called_once()


@pytest.mark.asyncio
async def test_health_check_failure():
    """Test that health_check returns False when connection fails."""
    mock_client = AsyncMock()
    mock_client.fetch_entries.side_effect = Exception("Connection error")

    source = MinifluxSource(mock_client)
    result = await source.health_check()

    assert result is False
