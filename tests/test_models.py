"""Tests for data models."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from cyris.domain.models import (
    Article,
    DigestContent,
    DigestItem,
    SourceConfig,
    StoredArticle,
    Tier,
)


class TestTier:
    def test_filter_value(self):
        assert Tier.FILTER == "filter"

    def test_summarize_value(self):
        assert Tier.SUMMARIZE == "summarize"

    def test_invalid_tier(self):
        with pytest.raises(ValueError):
            Tier("invalid")


class TestArticle:
    def test_valid_article(self):
        article = Article(
            id=1,
            title="Test Article",
            url="https://example.com/article",
            content="Article content here",
            published_at=datetime(2026, 3, 16, 10, 0, tzinfo=UTC),
            source_name="TechCrunch",
            source_tier=Tier.FILTER,
        )
        assert article.id == 1
        assert article.source_tier == Tier.FILTER
        assert article.source_tags == []
        assert article.author is None

    def test_article_with_all_fields(self):
        article = Article(
            id=2,
            title="Full Article",
            url="https://example.com/full",
            content="Full content",
            author="Jane Doe",
            published_at=datetime(2026, 3, 16, 10, 0, tzinfo=UTC),
            source_name="Stratechery",
            source_tier=Tier.SUMMARIZE,
            source_tags=["tech", "strategy"],
        )
        assert article.author == "Jane Doe"
        assert article.source_tags == ["tech", "strategy"]

    def test_article_missing_required_field(self):
        with pytest.raises(ValidationError):
            Article(
                id=1,
                title="Missing URL",
                content="Content",
                published_at=datetime.now(tz=UTC),
                source_name="Test",
                source_tier=Tier.FILTER,
            )


class TestStoredArticle:
    def test_article_roundtrip_preserves_ref_urls(self):
        article = Article(
            id=1,
            title="Newsletter",
            url="newsletter:abc",
            content="Content",
            published_at=datetime(2026, 3, 16, 10, 0, tzinfo=UTC),
            source_name="Newsletter",
            source_tier=Tier.SUMMARIZE,
            ref_urls=["https://example.com/a"],
        )

        stored = StoredArticle.from_article(article, first_seen_at=datetime.now(UTC))

        assert stored.to_article().ref_urls == ["https://example.com/a"]

    def test_old_stored_article_defaults_ref_urls_to_empty_list(self):
        article = Article(
            id=1,
            title="Newsletter",
            url="newsletter:abc",
            content="Content",
            published_at=datetime(2026, 3, 16, 10, 0, tzinfo=UTC),
            source_name="Newsletter",
            source_tier=Tier.SUMMARIZE,
        )
        old_data = StoredArticle.from_article(article, first_seen_at=datetime.now(UTC)).model_dump()
        old_data.pop("ref_urls", None)

        restored = StoredArticle.model_validate(old_data)

        assert restored.ref_urls == []
        assert restored.to_article().ref_urls == []


class TestSourceConfig:
    def test_defaults(self):
        source = SourceConfig(name="Test Source")
        assert source.tier == Tier.FILTER
        assert source.type == "rss"
        assert source.language == "auto"
        assert source.tags == []

    def test_summarize_source(self):
        source = SourceConfig(
            name="Stratechery",
            url="https://stratechery.com/feed/",
            tier=Tier.SUMMARIZE,
        )
        assert source.tier == Tier.SUMMARIZE
        assert source.url == "https://stratechery.com/feed/"


class TestDigestContent:
    def test_minimal_digest(self):
        digest = DigestContent(
            date="2026-03-17",
            period="morning",
            sources_processed=10,
            articles_received=50,
            articles_included=5,
        )
        assert digest.thematic_summaries == []
        assert digest.attention_sections == []
        assert digest.filtered_headlines == []
        assert digest.tracked_updates is None

    def test_full_digest(self, sample_digest_content):
        assert sample_digest_content.articles_included == 3
        assert len(sample_digest_content.thematic_summaries) == 1
        assert len(sample_digest_content.filtered_headlines) == 2

    def test_attention_sections_default(self):
        digest = DigestContent(
            date="2026-04-10",
            period="morning",
            sources_processed=5,
            articles_received=20,
            articles_included=10,
        )
        assert digest.attention_sections == []

    def test_attention_sections_explicit(self):
        from cyris.domain.models import DigestItem, DigestSection

        sections = [
            DigestSection(
                heading="tech",
                items=[
                    DigestItem(
                        title="Article 1",
                        summary="",
                        sources=["TechNews"],
                        urls=["https://example.com/1"],
                    )
                ],
            )
        ]
        digest = DigestContent(
            date="2026-04-10",
            period="evening",
            sources_processed=3,
            articles_received=15,
            articles_included=5,
            attention_sections=sections,
        )
        assert len(digest.attention_sections) == 1
        assert digest.attention_sections[0].heading == "tech"


class TestDigestItemLink:
    def test_prefers_ref_url(self):
        item = DigestItem(
            title="t",
            summary="s",
            sources=["曼報"],
            urls=["newsletter:abc"],
            ref_urls=["https://example.com/a"],
        )
        assert item.link == "https://example.com/a"

    def test_falls_back_to_store_url(self):
        item = DigestItem(title="t", summary="s", sources=["x"], urls=["https://example.com/b"])
        assert item.link == "https://example.com/b"

    def test_synthetic_store_url_is_not_a_link(self):
        item = DigestItem(title="t", summary="s", sources=["曼報"], urls=["newsletter:abc"])
        assert item.link is None
