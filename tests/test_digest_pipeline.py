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

    async def test_fan_tier_passthrough(self, pipeline, sample_sources):
        """Fan-tier articles bypass LLM, group by source, and are never discarded."""
        fan_articles = [
            Article(
                id=90,
                title="社團週報 #12",
                url="https://group.example/12",
                content="<p>本週活動整理與公告內容……</p>",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="某社團電子報",
                source_tier=Tier.FAN,
            ),
            Article(
                id=91,
                title="社團週報 #13",
                url="https://group.example/13",
                content="下週活動預告",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="某社團電子報",
                source_tier=Tier.FAN,
            ),
        ]

        # No filter/summarize mocks needed — fan tier never reaches the LLM path.
        result = await pipeline.process(fan_articles, sample_sources, period="morning")

        assert len(result.content.fan_sections) == 1  # grouped by the single source
        section = result.content.fan_sections[0]
        assert section.heading == "某社團電子報"
        assert len(section.items) == 2
        assert section.items[0].summary == "本週活動整理與公告內容……"  # HTML-stripped excerpt
        # Both fan URLs accepted, none rejected
        assert set(result.accepted_urls) == {"https://group.example/12", "https://group.example/13"}
        assert result.rejected_urls == []
        assert result.content.articles_included == 2

    async def test_story_records_keep_full_membership_past_truncation(self, sample_sources):
        """Story records carry every cluster's full URL list even when the cap drops clusters."""
        news = [
            Article(
                id=i,
                title=f"News {i}",
                url=f"https://news.example/{i}",
                content="News content",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="Wire",
                source_tier=Tier.FILTER,
                source_tags=["news"],
            )
            for i in (1, 2, 3)
        ]
        pipeline = DigestPipeline(
            FakeLLM(
                '{"clusters": ['
                '{"heading": "A", "summary": "S", "article_ids": [1, 2], "tags": []}, '
                '{"heading": "B", "summary": "S", "article_ids": [3], "tags": []}]}'
            ),
            max_digest_output=1,
        )

        result = await pipeline.process(news, sample_sources, period="morning")

        # The cap truncated the rendered clusters...
        assert len(result.content.news_clusters) == 1
        # ...but the records still name both stories with their full memberships.
        date = result.content.date
        assert [r.id for r in result.story_records] == [f"{date}-morning-0", f"{date}-morning-1"]
        assert result.story_records[0].urls == ["https://news.example/1", "https://news.example/2"]
        assert result.story_records[1].urls == ["https://news.example/3"]

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
