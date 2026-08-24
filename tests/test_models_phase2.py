"""Tests for Phase 2 model extensions."""

from datetime import datetime

from cyris.domain.models import Article, Tier


class TestArticleExtension:
    def test_string_id(self):
        a = Article(
            id="newsletter-abc123",
            title="T",
            url="https://example.com",
            content="C",
            published_at=datetime.now(),
            source_name="Test",
            source_tier=Tier.SUMMARIZE,
        )
        assert a.id == "newsletter-abc123"

    def test_int_id(self):
        a = Article(
            id=101,
            title="T",
            url="https://example.com",
            content="C",
            published_at=datetime.now(),
            source_name="Test",
            source_tier=Tier.FILTER,
        )
        assert a.id == 101
