"""Tests for DigestPipeline."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fakes import FakeLLM

from cyris.domain.models import Article, DigestItem, DigestSection, Tier
from cyris.domain.selection import split_summarize_tier_by_score
from cyris.service_layer.digest_pipeline import DigestPipeline


class TestDigestPipeline:
    @pytest.fixture
    def pipeline(self):
        return DigestPipeline(FakeLLM())

    async def test_process_mixed_articles(
        self,
        pipeline,
        sample_filter_articles,
        sample_summarize_articles,
        sample_sources,
    ):
        mock_filter_result = [
            DigestItem(
                title="Apple Vision Pro 第二代",
                summary="價格降至 $2499",
                sources=["TechCrunch"],
                urls=["https://techcrunch.com/2026/03/16/apple-vision-pro-2"],
            )
        ]
        mock_summarize_result = [
            DigestSection(
                heading="AI 趨勢",
                items=[
                    DigestItem(
                        title="AI regulation",
                        summary="歐盟 AI 法案推進",
                        sources=["Stratechery"],
                        urls=["https://stratechery.com/2026/03/16/weekly-trends"],
                    )
                ],
            )
        ]

        all_articles = sample_filter_articles + sample_summarize_articles

        with (
            patch(
                "cyris.service_layer.digest_pipeline.filter_articles",
                new_callable=AsyncMock,
                return_value=mock_filter_result,
            ),
            patch(
                "cyris.service_layer.digest_pipeline.summarize_articles",
                new_callable=AsyncMock,
                return_value=mock_summarize_result,
            ),
        ):
            result = await pipeline.process(all_articles, sample_sources, period="morning")

        assert result.content.period == "morning"
        assert result.content.articles_received == 3
        assert result.content.sources_processed == 3
        assert result.content.articles_included == 2  # 1 filtered + 1 summarized
        assert len(result.content.filtered_headlines) == 1
        assert len(result.content.thematic_summaries) == 1
        # Check URL classification
        assert "https://techcrunch.com/2026/03/16/apple-vision-pro-2" in result.accepted_urls
        assert "https://stratechery.com/2026/03/16/weekly-trends" in result.accepted_urls
        assert "https://reuters.com/2026/03/16/tsmc-arizona" in result.rejected_urls

    async def test_process_no_articles(self, pipeline, sample_sources):
        result = await pipeline.process([], sample_sources, period="evening")

        assert result.content.articles_received == 0
        assert result.content.articles_included == 0
        assert result.content.filtered_headlines == []
        assert result.content.thematic_summaries == []
        assert result.accepted_urls == []
        assert result.rejected_urls == []


class TestSplitSummarizeTierByScore:
    def test_all_above_threshold(self):
        articles = [
            Article(
                id=1,
                title="High Score 1",
                url="https://example.com/1",
                content="Content",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="Source",
                source_tier=Tier.SUMMARIZE,
            ),
            Article(
                id=2,
                title="High Score 2",
                url="https://example.com/2",
                content="Content",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="Source",
                source_tier=Tier.SUMMARIZE,
            ),
        ]
        article_scores = {
            "https://example.com/1": 80.0,
            "https://example.com/2": 75.0,
        }

        high, low = split_summarize_tier_by_score(articles, article_scores, threshold=70)

        assert len(high) == 2
        assert len(low) == 0

    def test_mixed_scores(self):
        articles = [
            Article(
                id=1,
                title="High",
                url="https://example.com/1",
                content="Content",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="Source",
                source_tier=Tier.SUMMARIZE,
            ),
            Article(
                id=2,
                title="Low 1",
                url="https://example.com/2",
                content="Content",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="Source",
                source_tier=Tier.SUMMARIZE,
            ),
            Article(
                id=3,
                title="Low 2",
                url="https://example.com/3",
                content="Content",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="Source",
                source_tier=Tier.SUMMARIZE,
            ),
        ]
        article_scores = {
            "https://example.com/1": 80.0,
            "https://example.com/2": 55.0,
            "https://example.com/3": 65.0,
        }

        high, low = split_summarize_tier_by_score(articles, article_scores, threshold=70)

        assert len(high) == 1
        assert high[0].title == "High"
        assert len(low) == 2
        assert low[0].title == "Low 1"
        assert low[1].title == "Low 2"

    def test_missing_scores_default_to_high(self):
        articles = [
            Article(
                id=1,
                title="Has Score",
                url="https://example.com/1",
                content="Content",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="Source",
                source_tier=Tier.SUMMARIZE,
            ),
            Article(
                id=2,
                title="No Score",
                url="https://example.com/2",
                content="Content",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="Source",
                source_tier=Tier.SUMMARIZE,
            ),
        ]
        article_scores = {
            "https://example.com/1": 55.0,
        }

        high, low = split_summarize_tier_by_score(articles, article_scores, threshold=70)

        assert len(high) == 1
        assert high[0].title == "No Score"
        assert len(low) == 1
        assert low[0].title == "Has Score"

    def test_none_article_scores_all_go_to_high(self):
        articles = [
            Article(
                id=1,
                title="Article 1",
                url="https://example.com/1",
                content="Content",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="Source",
                source_tier=Tier.SUMMARIZE,
            ),
            Article(
                id=2,
                title="Article 2",
                url="https://example.com/2",
                content="Content",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="Source",
                source_tier=Tier.SUMMARIZE,
            ),
        ]

        high, low = split_summarize_tier_by_score(articles, None, threshold=70)

        assert len(high) == 2
        assert len(low) == 0

    def test_empty_articles(self):
        high, low = split_summarize_tier_by_score([], {}, threshold=70)

        assert high == []
        assert low == []

    def test_score_exactly_at_threshold(self):
        articles = [
            Article(
                id=1,
                title="Exact",
                url="https://example.com/1",
                content="Content",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="Source",
                source_tier=Tier.SUMMARIZE,
            ),
        ]
        article_scores = {"https://example.com/1": 70.0}

        high, low = split_summarize_tier_by_score(articles, article_scores, threshold=70)

        assert len(high) == 1
        assert len(low) == 0
