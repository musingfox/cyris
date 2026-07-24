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


class TestFanTierBodyArticle:
    @pytest.fixture
    def fan_source(self):
        return SourceConfig(name="粉虱通訊", tier=Tier.FAN, tags=["music", "culture"])

    @pytest.mark.asyncio
    async def test_fan_email_body_becomes_one_article(self, fan_source):
        parsed = ParsedNewsletter(
            source_name="粉虱通訊",
            subject="粉虱通訊 No. 28",
            from_email="sorryyouth@166558258.mailchimpapp.com",
            date=datetime(2026, 7, 24),
            links=["https://mailchi.mp/abc/no-28", "https://example.com/song"],
            html_content="<p>本期開場白，內容從略&#8230;</p>",
            text_content="本期開場白，內容從略……",
        )
        with patch("cyris.adapters.fetch.newsletter.extract_full_text") as mock_extract:
            articles = await fetch_newsletter_articles(parsed, fan_source, AsyncMock())

        mock_extract.assert_not_called()
        assert len(articles) == 1
        article = articles[0]
        assert article.title == "粉虱通訊 No. 28"
        assert article.url == "https://mailchi.mp/abc/no-28"
        assert article.content == "本期開場白，內容從略……"
        assert article.source_tier == Tier.FAN

    @pytest.mark.asyncio
    async def test_fan_email_without_text_part_strips_html(self, fan_source):
        parsed = ParsedNewsletter(
            source_name="粉虱通訊",
            subject="粉虱通訊 No. 29",
            from_email="sorryyouth@166558258.mailchimpapp.com",
            date=datetime(2026, 8, 1),
            links=[],
            html_content="<div><p>Hello &amp; goodbye</p></div>",
            text_content="",
        )
        articles = await fetch_newsletter_articles(parsed, fan_source, AsyncMock())

        assert len(articles) == 1
        assert articles[0].content == "Hello & goodbye"
        assert articles[0].url.startswith("newsletter:")
