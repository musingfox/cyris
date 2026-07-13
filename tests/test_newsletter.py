"""Tests for newsletter article fetching."""

import hashlib
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from cyris.adapters.fetch.email_parser import ParsedNewsletter
from cyris.adapters.fetch.extractor import ExtractedContent
from cyris.adapters.fetch.newsletter import _generate_article_id, fetch_newsletter_articles
from cyris.domain.models import SourceConfig, Tier


@pytest.fixture
def parsed():
    return ParsedNewsletter(
        source_name="Test",
        subject="Issue #1",
        from_email="list@example.com",
        date=datetime(2026, 3, 18),
        links=["https://example.com/1", "https://example.com/2", "https://example.com/3"],
        html_content="",
        text_content="",
    )


@pytest.fixture
def source():
    return SourceConfig(name="Test Newsletter", tier=Tier.SUMMARIZE, tags=["tech"])


class TestGenerateArticleId:
    def test_deterministic(self):
        expected = hashlib.sha256(b"Test Newsletterhttps://example.com/1").hexdigest()
        assert _generate_article_id("Test Newsletter", "https://example.com/1") == expected


class TestFetchNewsletterArticles:
    @pytest.mark.asyncio
    async def test_fetches_all_links(self, parsed, source):
        mock_client = AsyncMock()

        async def mock_extract(url, client, cookies=None):
            return ExtractedContent(
                url=url,
                title=f"Title for {url}",
                content="Content",
                author=None,
                published_at=None,
                raw_html="",
            )

        with patch("cyris.adapters.fetch.newsletter.extract_full_text", side_effect=mock_extract):
            articles = await fetch_newsletter_articles(parsed, source, mock_client)

        assert len(articles) == 3

    @pytest.mark.asyncio
    async def test_skips_empty_content(self, parsed, source):
        mock_client = AsyncMock()

        async def mock_extract(url, client, cookies=None):
            return ExtractedContent(
                url=url, title="", content="", author=None, published_at=None, raw_html=""
            )

        with patch("cyris.adapters.fetch.newsletter.extract_full_text", side_effect=mock_extract):
            articles = await fetch_newsletter_articles(parsed, source, mock_client)

        assert len(articles) == 0
