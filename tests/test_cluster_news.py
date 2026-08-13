"""Tests for news clustering."""

from datetime import UTC, datetime

import pytest
from fakes import FakeLLM

from cyris.domain.models import Article, Tier, UsageStats
from cyris.service_layer.cluster_news import cluster_news, filter_news


class TestFilterNews:
    def test_filter_news_mixed_tags(self):
        articles = [
            Article(
                id=1,
                title="Breaking: Major Event",
                url="https://example.com/1",
                content="Content 1",
                published_at=datetime(2026, 3, 31, 10, 0, tzinfo=UTC),
                source_name="Reuters",
                source_tier=Tier.FILTER,
                source_tags=["international", "news"],
            ),
            Article(
                id=2,
                title="Tech Startup Launch",
                url="https://example.com/2",
                content="Content 2",
                published_at=datetime(2026, 3, 31, 11, 0, tzinfo=UTC),
                source_name="TechCrunch",
                source_tier=Tier.FILTER,
                source_tags=["tech", "startup"],
            ),
        ]

        news_articles, non_news_articles = filter_news(articles)

        assert len(news_articles) == 1
        assert len(non_news_articles) == 1
        assert news_articles[0].id == 1
        assert non_news_articles[0].id == 2

    def test_filter_news_tag_position(self):
        article = Article(
            id=1,
            title="Investigative Report",
            url="https://example.com/1",
            content="Content",
            published_at=datetime(2026, 3, 31, 10, 0, tzinfo=UTC),
            source_name="ProPublica",
            source_tier=Tier.FILTER,
            source_tags=["news", "investigative"],
        )

        news_articles, non_news_articles = filter_news([article])

        assert len(news_articles) == 1
        assert len(non_news_articles) == 0

    def test_filter_news_case_sensitive(self):
        article = Article(
            id=1,
            title="Breaking News",
            url="https://example.com/1",
            content="Content",
            published_at=datetime(2026, 3, 31, 10, 0, tzinfo=UTC),
            source_name="News Source",
            source_tier=Tier.FILTER,
            source_tags=["NEWS"],
        )

        news_articles, non_news_articles = filter_news([article])

        assert len(news_articles) == 0
        assert len(non_news_articles) == 1


class TestClusterNews:
    @pytest.fixture
    def sample_news_articles(self):
        return [
            Article(
                id=101,
                title="Tech Company Announces Layoffs",
                url="https://example.com/101",
                content="A major tech company announced layoffs today affecting 1000 employees.",
                published_at=datetime(2026, 3, 31, 10, 0, tzinfo=UTC),
                source_name="Reuters",
                source_tier=Tier.FILTER,
                source_tags=["tech", "news"],
            ),
            Article(
                id=102,
                title="Another Tech Firm Cuts Jobs",
                url="https://example.com/102",
                content="Following the trend, another tech firm announced workforce reduction.",
                published_at=datetime(2026, 3, 31, 11, 0, tzinfo=UTC),
                source_name="Bloomberg",
                source_tier=Tier.FILTER,
                source_tags=["business", "news"],
            ),
            Article(
                id=103,
                title="New AI Model Released",
                url="https://example.com/103",
                content="An independent story about a new AI model from OpenAI.",
                published_at=datetime(2026, 3, 31, 12, 0, tzinfo=UTC),
                source_name="TechCrunch",
                source_tier=Tier.FILTER,
                source_tags=["ai", "news"],
            ),
        ]

    async def test_cluster_news_basic(self, sample_news_articles):
        llm = FakeLLM(
            """{
                "clusters": [
                    {
                        "heading": "科技業裁員潮",
                        "summary": "多家科技公司宣布裁員，影響員工。業界面臨壓力。",
                        "article_ids": [101, 102]
                    }
                ],
                "unclustered_ids": [103]
            }""",
            input_tokens=100,
            output_tokens=50,
        )

        usage = UsageStats(model="claude-sonnet-4-6")
        clusters, unclustered = await cluster_news(
            sample_news_articles,
            llm,
            usage=usage,
        )

        assert len(clusters) == 1
        assert clusters[0].heading == "科技業裁員潮"
        assert len(clusters[0].items) == 1
        assert len(clusters[0].items[0].sources) == 2
        assert "Reuters" in clusters[0].items[0].sources
        assert "Bloomberg" in clusters[0].items[0].sources

        assert len(unclustered) == 1
        assert unclustered[0].id == 103

        assert usage.input_tokens == 100
        assert usage.output_tokens == 50

    async def test_cluster_news_merges_reference_urls_with_store_fallback(self):
        articles = [
            Article(
                id=201,
                title="Store article",
                url="https://store.example/a",
                content="A",
                published_at=datetime(2026, 3, 31, 10, 0, tzinfo=UTC),
                source_name="Source A",
                source_tier=Tier.FILTER,
                source_tags=["news"],
            ),
            Article(
                id=202,
                title="Newsletter article",
                url="newsletter:202",
                content="B",
                published_at=datetime(2026, 3, 31, 11, 0, tzinfo=UTC),
                source_name="Newsletter",
                source_tier=Tier.FILTER,
                source_tags=["news"],
                ref_urls=["https://ref.example/one", "https://ref.example/two"],
            ),
        ]
        llm = FakeLLM(
            '{"clusters": [{"heading": "Mixed", "summary": "s", "article_ids": [201, 202]}], '
            '"unclustered_ids": []}'
        )

        clusters, unclustered = await cluster_news(articles, llm)

        item = clusters[0].items[0]
        assert item.ref_urls == [
            "https://store.example/a",
            "https://ref.example/one",
            "https://ref.example/two",
        ]
        assert item.urls == ["https://store.example/a", "newsletter:202"]
        assert item.sources == ["Source A", "Newsletter"]
        assert unclustered == []

    async def test_cluster_news_drops_synthetic_urls_from_reference_fallback(self):
        articles = [
            Article(
                id=207,
                title="Newsletter without refs",
                url="newsletter:207",
                content="A",
                published_at=datetime(2026, 3, 31, 10, 0, tzinfo=UTC),
                source_name="Newsletter A",
                source_tier=Tier.FILTER,
                source_tags=["news"],
            ),
            Article(
                id=208,
                title="Newsletter with refs",
                url="newsletter:208",
                content="B",
                published_at=datetime(2026, 3, 31, 11, 0, tzinfo=UTC),
                source_name="Newsletter B",
                source_tier=Tier.FILTER,
                source_tags=["news"],
                ref_urls=["https://ref.example/x"],
            ),
        ]
        llm = FakeLLM(
            '{"clusters": [{"heading": "Syn", "summary": "s", "article_ids": [207, 208]}], '
            '"unclustered_ids": []}'
        )

        clusters, _ = await cluster_news(articles, llm)

        item = clusters[0].items[0]
        assert item.ref_urls == ["https://ref.example/x"]
        assert item.urls == ["newsletter:207", "newsletter:208"]

    async def test_cluster_news_dedups_reference_urls_across_members(self):
        articles = [
            Article(
                id=205,
                title="Newsletter one",
                url="newsletter:205",
                content="A",
                published_at=datetime(2026, 3, 31, 10, 0, tzinfo=UTC),
                source_name="Newsletter A",
                source_tier=Tier.FILTER,
                source_tags=["news"],
                ref_urls=["https://ref.example/shared", "https://ref.example/only-a"],
            ),
            Article(
                id=206,
                title="Newsletter two",
                url="newsletter:206",
                content="B",
                published_at=datetime(2026, 3, 31, 11, 0, tzinfo=UTC),
                source_name="Newsletter B",
                source_tier=Tier.FILTER,
                source_tags=["news"],
                ref_urls=["https://ref.example/shared", "https://ref.example/only-b"],
            ),
        ]
        llm = FakeLLM(
            '{"clusters": [{"heading": "Dup", "summary": "s", "article_ids": [205, 206]}], '
            '"unclustered_ids": []}'
        )

        clusters, _ = await cluster_news(articles, llm)

        item = clusters[0].items[0]
        assert item.ref_urls == [
            "https://ref.example/shared",
            "https://ref.example/only-a",
            "https://ref.example/only-b",
        ]
        assert item.urls == ["newsletter:205", "newsletter:206"]

    async def test_cluster_news_keeps_reference_urls_empty_without_references(self):
        articles = [
            Article(
                id=203,
                title="First",
                url="https://store.example/first",
                content="A",
                published_at=datetime(2026, 3, 31, 10, 0, tzinfo=UTC),
                source_name="Source A",
                source_tier=Tier.FILTER,
                source_tags=["news"],
            ),
            Article(
                id=204,
                title="Second",
                url="https://store.example/second",
                content="B",
                published_at=datetime(2026, 3, 31, 11, 0, tzinfo=UTC),
                source_name="Source B",
                source_tier=Tier.FILTER,
                source_tags=["news"],
            ),
        ]
        llm = FakeLLM(
            '{"clusters": [{"heading": "Plain", "summary": "s", "article_ids": [203, 204]}], '
            '"unclustered_ids": []}'
        )

        clusters, unclustered = await cluster_news(articles, llm)

        item = clusters[0].items[0]
        assert item.ref_urls == []
        assert item.urls == ["https://store.example/first", "https://store.example/second"]
        assert item.sources == ["Source A", "Source B"]
        assert unclustered == []

    async def test_cluster_news_no_clusters(self, sample_news_articles):
        llm = FakeLLM('{"clusters": [], "unclustered_ids": [101, 102, 103]}')

        clusters, unclustered = await cluster_news(sample_news_articles, llm)

        assert len(clusters) == 0
        assert len(unclustered) == 3

    async def test_cluster_news_empty_input(self):
        clusters, unclustered = await cluster_news([], FakeLLM())

        assert clusters == []
        assert unclustered == []

    async def test_cluster_news_api_failure(self, sample_news_articles):
        llm = FakeLLM(error=Exception("API Error"))

        clusters, unclustered = await cluster_news(sample_news_articles, llm)

        assert len(clusters) == 0
        assert len(unclustered) == 3
        assert unclustered == sample_news_articles
