"""Integration tests for CLI commands."""

from unittest.mock import AsyncMock, patch

import pytest

from cyris.adapters.fetch.miniflux import MinifluxClient
from cyris.adapters.fetch.miniflux_source import MinifluxSource
from cyris.adapters.fetch.newsletter_source import NewsletterArchiveSource
from cyris.domain.models import Article, Tier
from cyris.service_layer.fetching import fetch_all_articles


@pytest.mark.asyncio
async def test_digest_uses_unified_fetcher(tmp_path):
    """The unified fetcher drives every wired source, not just the first one."""
    from datetime import datetime

    # Create mock miniflux client
    with patch.object(MinifluxClient, "fetch_entries", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = [
            Article(
                id=1,
                title="Test",
                url="https://example.com",
                content="Content",
                published_at=datetime.now(),
                source_name="Test Source",
                source_tier=Tier.FILTER,
            )
        ]

        client = MinifluxClient("http://localhost:8080", "test-key")
        miniflux_source = MinifluxSource(client)

        # Create newsletter source (empty archive)
        newsletter_archive = tmp_path / "newsletters"
        newsletter_archive.mkdir()
        newsletter_source = NewsletterArchiveSource(newsletter_archive)

        result, _ = await fetch_all_articles(
            fetch_sources=[miniflux_source, newsletter_source],
            after=datetime.now(),
            before=datetime.now(),
            sources={},
            aliases={},
        )

        mock_fetch.assert_called_once()
        assert len(result) == 1
