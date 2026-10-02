"""Tests for DigestPipeline."""

import hashlib
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fakes import TEST_SETTINGS, FakeLLM, pipeline_settings

from cyris.adapters.output.html_digest import HtmlDigestWriter
from cyris.domain.models import Article, DigestItem, DigestSection, Tier
from cyris.domain.selection import split_summarize_tier_by_score
from cyris.service_layer.digest_pipeline import DigestPipeline

pytestmark = pytest.mark.integration


def _expected_story_id(date: str, period: str, urls: list[str]) -> str:
    """Computed independently of the implementation: the id formula is a contract."""
    return f"{date}-{period}-" + hashlib.sha1("\n".join(sorted(urls)).encode()).hexdigest()[:8]


class TestDigestPipeline:
    @pytest.fixture
    def pipeline(self):
        return DigestPipeline(FakeLLM(), **pipeline_settings())

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
            result = await pipeline.process(
                all_articles,
                sample_sources,
                period="morning",
                timezone=TEST_SETTINGS["general.timezone"],
            )

        assert result.content.period == "morning"
        assert result.content.articles_received == 3
        assert result.content.sources_processed == 3
        assert result.content.articles_included == 2  # 1 filtered + 1 summarized
        assert len(result.content.filtered_headlines) == 1
        assert result.content.thematic_summaries == []
        assert [len(s.items) for s in result.content.featured_articles] == [1]
        # Check URL classification
        assert "https://techcrunch.com/2026/03/16/apple-vision-pro-2" in result.accepted_urls
        assert "https://stratechery.com/2026/03/16/weekly-trends" in result.accepted_urls
        assert "https://reuters.com/2026/03/16/tsmc-arizona" in result.rejected_urls

    @pytest.mark.parametrize("fan_articles", [0, 1], ids=["no-articles", "one-article"])
    async def test_the_content_records_the_language_it_was_written_in(
        self, sample_sources, fan_articles
    ):
        pipeline = DigestPipeline(FakeLLM(), **pipeline_settings(output_language="ja"))
        articles = [
            Article(
                id=90,
                title="Weekly",
                url="https://group.example/12",
                content="Notes.",
                published_at=datetime(2026, 4, 10, tzinfo=UTC),
                source_name="Fan source",
                source_tier=Tier.FAN,
            )
        ][:fan_articles]

        result = await pipeline.process(
            articles, sample_sources, period="morning", timezone=TEST_SETTINGS["general.timezone"]
        )

        assert result.content.output_language == "ja"

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
        result = await pipeline.process(
            fan_articles,
            sample_sources,
            period="morning",
            timezone=TEST_SETTINGS["general.timezone"],
        )

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
                '{"heading": "A", "summary": "S", "article_ids": [0, 1], "tags": []}, '
                '{"heading": "B", "summary": "S", "article_ids": [2], "tags": []}]}'
            ),
            **pipeline_settings(max_digest_output=1),
        )

        result = await pipeline.process(
            news, sample_sources, period="morning", timezone=TEST_SETTINGS["general.timezone"]
        )

        # The cap truncated the rendered clusters...
        assert len(result.content.news_clusters) == 1
        # ...but the records still name both stories with their full memberships,
        # under content-derived ids a re-run of the same window would reproduce.
        date = result.content.date
        assert [r.id for r in result.story_records] == [
            _expected_story_id(
                date, "morning", ["https://news.example/1", "https://news.example/2"]
            ),
            _expected_story_id(date, "morning", ["https://news.example/3"]),
        ]
        assert result.story_records[0].urls == ["https://news.example/1", "https://news.example/2"]
        assert result.story_records[1].urls == ["https://news.example/3"]

    async def test_rendered_story_id_matches_persisted_record(self, sample_sources, tmp_path):
        """Seam: the id the digest renders is the id the story store persists."""
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
                '{"heading": "A", "summary": "S", "article_ids": [0, 1], "tags": []}, '
                '{"heading": "B", "summary": "S", "article_ids": [2], "tags": []}]}'
            ),
            **pipeline_settings(),
        )

        result = await pipeline.process(
            news, sample_sources, period="morning", timezone=TEST_SETTINGS["general.timezone"]
        )

        # (a) Sections and records carry the same ids, one-to-one, in order.
        assert [s.story_id for s in result.content.news_clusters] == [
            r.id for r in result.story_records
        ]

        # (b) The rendered page exposes each of those ids as data-story-id, verbatim.
        writer = HtmlDigestWriter(tmp_path)
        html = writer.render(result.content)
        for record in result.story_records:
            assert f'data-story-id="{record.id}"' in html

    async def test_process_no_articles(self, pipeline, sample_sources):
        result = await pipeline.process(
            [], sample_sources, period="evening", timezone=TEST_SETTINGS["general.timezone"]
        )

        assert result.content.articles_received == 0
        assert result.content.articles_included == 0
        assert result.content.filtered_headlines == []
        assert result.content.thematic_summaries == []
        assert result.accepted_urls == []
        assert result.rejected_urls == []


def _article(article_id: int, tier: Tier, source_name: str) -> Article:
    return Article(
        id=article_id,
        title=f"Article {article_id}",
        url=f"https://example.com/{article_id}",
        content="Body.",
        published_at=datetime(2026, 4, 10, tzinfo=UTC),
        source_name=source_name,
        source_tier=tier,
    )


def _item(article: Article) -> DigestItem:
    return DigestItem(
        title=article.title,
        summary="Summary.",
        sources=[article.source_name],
        urls=[article.url],
    )


class TestVerdictsFollowTheCappedDigest:
    """Accepted means shown in this issue; what the issue left out stays pending."""

    async def _process(self, articles, sample_sources, *, headlines, sections, cap):
        pipeline = DigestPipeline(FakeLLM(), **pipeline_settings(max_digest_output=cap))
        with (
            patch(
                "cyris.service_layer.digest_pipeline.filter_articles",
                new_callable=AsyncMock,
                return_value=headlines,
            ),
            patch(
                "cyris.service_layer.digest_pipeline.summarize_articles",
                new_callable=AsyncMock,
                return_value=sections,
            ),
        ):
            return await pipeline.process(
                articles, sample_sources, timezone=TEST_SETTINGS["general.timezone"]
            )

    async def test_a_headline_the_cap_cut_is_neither_accepted_nor_rejected(self, sample_sources):
        shown, cut, discarded = (_article(i, Tier.FILTER, "TechCrunch") for i in (1, 2, 3))

        result = await self._process(
            [shown, cut, discarded],
            sample_sources,
            headlines=[_item(shown), _item(cut)],
            sections=[],
            cap=1,
        )

        assert result.accepted_urls == [shown.url]
        assert result.rejected_urls == [discarded.url]

    async def test_a_summary_item_the_cap_cut_is_neither_accepted_nor_rejected(
        self, sample_sources
    ):
        shown, cut = (_article(i, Tier.SUMMARIZE, "Stratechery") for i in (1, 2))

        result = await self._process(
            [shown, cut],
            sample_sources,
            headlines=[],
            sections=[DigestSection(heading="Theme", items=[_item(shown), _item(cut)])],
            cap=1,
        )

        assert result.accepted_urls == [shown.url]
        assert result.rejected_urls == []

    async def test_a_summarize_article_no_section_names_is_neither_accepted_nor_rejected(
        self, sample_sources
    ):
        named, left_out = (_article(i, Tier.SUMMARIZE, "Stratechery") for i in (1, 2))

        result = await self._process(
            [named, left_out],
            sample_sources,
            headlines=[],
            sections=[DigestSection(heading="Theme", items=[_item(named)])],
            cap=15,
        )

        assert result.accepted_urls == [named.url]
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


def _scored_topic() -> tuple[list[Article], dict[str, float], FakeLLM]:
    """Three summarize-tier articles on one tag, and a model that writes both summary kinds."""
    articles = [
        Article(
            id=n,
            title=f"Essay {n}",
            url=f"https://essays.example/{n}",
            content=f"Essay {n} body.",
            published_at=datetime(2026, 4, 10, tzinfo=UTC),
            source_name="Stratechery",
            source_tier=Tier.SUMMARIZE,
            source_tags=["tech"],
        )
        for n in range(3)
    ]
    scores = {"https://essays.example/0": 90, "https://essays.example/1": 80}
    scores["https://essays.example/2"] = 60
    llm = FakeLLM(
        json.dumps(
            {
                "sections": [
                    {
                        "heading": "Topic heading",
                        "summary": "The group summary.",
                        "summaries": {str(n): f"Essay {n} on its own." for n in range(3)},
                        "article_ids": [0, 1, 2],
                    }
                ]
            }
        )
    )
    return articles, scores, llm


async def _page(sample_sources, tmp_path, **settings) -> str:
    articles, scores, llm = _scored_topic()
    pipeline = DigestPipeline(llm, **pipeline_settings(score_threshold=0, **settings))
    result = await pipeline.process(
        articles,
        sample_sources,
        timezone=TEST_SETTINGS["general.timezone"],
        article_scores=scores,
    )
    html = HtmlDigestWriter(tmp_path).render(result.content)
    return html[html.index("<main>") : html.index("</main>")]


class TestTheLayeringSettingsReachThePage:
    async def test_the_featured_score_decides_whether_the_top_story_is_a_group(
        self, sample_sources, tmp_path
    ):
        grouped = await _page(sample_sources, tmp_path, featured_threshold=70)
        single = await _page(sample_sources, tmp_path, featured_threshold=85)

        assert grouped.count("The group summary.") == 1
        assert "Topic heading" in grouped
        assert grouped.count('class="featured-item"') == 1
        assert "Essay 2 on its own." in grouped

        assert "The group summary." not in single
        assert "Topic heading" not in single
        assert single.count('class="featured-item"') == 2
        for n in range(3):
            assert f"Essay {n} on its own." in single

    async def test_max_featured_decides_how_many_features_the_page_shows(
        self, sample_sources, tmp_path
    ):
        one = await _page(sample_sources, tmp_path, featured_threshold=95, max_featured=1)
        two = await _page(sample_sources, tmp_path, featured_threshold=95, max_featured=2)

        assert one.count('class="featured-item"') == 1
        assert "Essay 2" not in one
        assert two.count('class="featured-item"') == 2
        assert "Essay 2 on its own." in two

    async def test_the_cap_runs_after_layering_and_keeps_the_group_whole(
        self, sample_sources, tmp_path
    ):
        page = await _page(sample_sources, tmp_path, featured_threshold=70, max_digest_output=1)

        assert "The group summary." not in page
        assert "Essay 0 on its own." in page
        assert "Essay 1" not in page and "Essay 2" not in page


@pytest.mark.parametrize(
    ("settings", "shown"),
    [
        ({"featured_threshold": 95, "max_featured": 1}, {0, 1}),
        ({"featured_threshold": 70, "max_digest_output": 1}, {0}),
    ],
    ids=["past-max-featured", "group-replaced-by-its-best"],
)
async def test_an_article_layering_or_the_cap_leaves_out_stays_pending(
    sample_sources, settings, shown
):
    articles, scores, llm = _scored_topic()
    pipeline = DigestPipeline(llm, **pipeline_settings(score_threshold=0, **settings))

    result = await pipeline.process(
        articles,
        sample_sources,
        timezone=TEST_SETTINGS["general.timezone"],
        article_scores=scores,
    )

    assert set(result.accepted_urls) == {f"https://essays.example/{n}" for n in shown}
    assert result.rejected_urls == []
