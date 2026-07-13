"""Tests for Miniflux client."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from cyris.adapters.fetch.miniflux import MinifluxClient
from cyris.domain.models import Tier


class TestMinifluxClient:
    @pytest.fixture
    def client(self):
        with patch("cyris.adapters.fetch.miniflux.miniflux.Client"):
            return MinifluxClient("http://localhost:8080", "test-key")

    async def test_fetch_entries(self, client, miniflux_response, sample_sources):
        client._client.get_entries = MagicMock(return_value=miniflux_response)

        articles = await client.fetch_entries(
            after=datetime(2026, 3, 16, 0, 0, tzinfo=UTC),
            before=datetime(2026, 3, 17, 0, 0, tzinfo=UTC),
            sources=sample_sources,
        )

        assert len(articles) == 3
        # TechCrunch article should get filter tier from sources config
        tc_article = next(a for a in articles if a.source_name == "TechCrunch")
        assert tc_article.source_tier == Tier.FILTER
        assert tc_article.id == 101

        # Stratechery article should get summarize tier
        strat_article = next(a for a in articles if a.source_name == "Stratechery")
        assert strat_article.source_tier == Tier.SUMMARIZE

    async def test_fetch_entries_empty(self, client, sample_sources):
        client._client.get_entries = MagicMock(return_value={"total": 0, "entries": []})

        articles = await client.fetch_entries(
            after=datetime(2026, 3, 16, 0, 0, tzinfo=UTC),
            before=datetime(2026, 3, 17, 0, 0, tzinfo=UTC),
            sources=sample_sources,
        )

        assert articles == []

    async def test_fetch_entries_unknown_source_defaults_to_filter(self, client, sample_sources):
        """Articles from unknown sources should default to filter tier."""
        client._client.get_entries = MagicMock(
            return_value={
                "total": 1,
                "entries": [
                    {
                        "id": 999,
                        "title": "Unknown Source Article",
                        "url": "https://unknown.com/article",
                        "content": "Content",
                        "published_at": "2026-03-16T10:00:00Z",
                        "feed": {"id": 99, "title": "Unknown Feed"},
                    }
                ],
            }
        )

        articles = await client.fetch_entries(
            after=datetime(2026, 3, 16, 0, 0, tzinfo=UTC),
            before=datetime(2026, 3, 17, 0, 0, tzinfo=UTC),
            sources=sample_sources,
        )

        assert len(articles) == 1
        assert articles[0].source_tier == Tier.FILTER
        assert articles[0].source_tags == []

    async def test_mark_as_read(self, client):
        client._client.update_entries = MagicMock()

        await client.mark_as_read([101, 102, 103])

        client._client.update_entries.assert_called_once_with([101, 102, 103], "read")

    async def test_mark_as_read_empty(self, client):
        client._client.update_entries = MagicMock()

        await client.mark_as_read([])

        client._client.update_entries.assert_not_called()
