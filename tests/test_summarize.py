"""Tests for read-lane processor."""

import json
from datetime import UTC

import pytest
from fakes import FakeLLM

from cyris.domain.models import Lane, UsageStats
from cyris.service_layer.degrade import excerpt
from cyris.service_layer.summarize import _group_by_tags, summarize_articles

pytestmark = pytest.mark.unit


class TestGroupByTags:
    def test_groups_by_first_tag(self, sample_filter_articles, sample_summarize_articles):
        all_articles = sample_filter_articles + sample_summarize_articles
        groups = _group_by_tags(all_articles)

        assert "tech" in groups
        assert "international" in groups
        assert len(groups["tech"]) == 2  # TechCrunch + Stratechery
        assert len(groups["international"]) == 1

    def test_no_tags_goes_to_general(self):
        from datetime import datetime

        from cyris.domain.models import Article

        article = Article(
            id=1,
            title="No tags",
            url="https://example.com",
            content="Content",
            published_at=datetime(2026, 3, 16, tzinfo=UTC),
            source_name="Unknown",
            source_lane=Lane.READ,
            source_tags=[],
        )
        groups = _group_by_tags([article])
        assert "general" in groups


class TestSummarizeArticles:
    @pytest.mark.parametrize(
        ("llm", "fell_back"),
        [
            (None, True),
            (FakeLLM(error=RuntimeError("quota")), True),
            (FakeLLM(json.dumps({"sections": []})), False),
        ],
        ids=["no-client", "call-failed", "answered"],
    )
    async def test_only_a_fallback_marks_the_usage(self, sample_summarize_articles, llm, fell_back):
        usage = UsageStats(model="m")

        await summarize_articles(
            sample_summarize_articles,
            llm,
            usage=usage,
            snippet_length=1000,
            output_language="zh-Hant",
            style_prompt="",
        )

        assert usage.fell_back_to_excerpts is fell_back

    async def test_summarize_returns_sections(self, sample_summarize_articles):
        llm = FakeLLM(
            json.dumps(
                {
                    "sections": [
                        {
                            "heading": "AI 監管趨勢",
                            "summary": "歐盟 AI 法案持續推進",
                            "article_ids": [0],
                        }
                    ]
                }
            )
        )

        sections = await summarize_articles(
            sample_summarize_articles,
            llm,
            snippet_length=1000,
            output_language="zh-Hant",
            style_prompt="",
        )

        assert len(sections) == 1
        assert sections[0].heading == "AI 監管趨勢"
        assert len(sections[0].items) == 1

    async def test_a_group_carries_its_one_summary_on_the_section(self, sample_summarize_articles):
        second = sample_summarize_articles[0].model_copy(
            update={"id": 104, "url": "https://stratechery.com/2026/03/17/other"}
        )
        llm = FakeLLM(
            json.dumps(
                {"sections": [{"heading": "H", "summary": "One summary", "article_ids": [0, 1]}]}
            )
        )

        sections = await summarize_articles(
            [*sample_summarize_articles, second],
            llm,
            snippet_length=1000,
            output_language="zh-Hant",
            style_prompt="",
        )

        assert sections[0].summary == "One summary"
        assert len(sections[0].items) == 2

    async def test_each_article_takes_its_own_summary_beside_the_groups(
        self, sample_summarize_articles
    ):
        second = sample_summarize_articles[0].model_copy(
            update={"id": 104, "url": "https://stratechery.com/2026/03/17/other"}
        )
        usage = UsageStats(model="m")
        llm = FakeLLM(
            json.dumps(
                {
                    "sections": [
                        {
                            "heading": "H",
                            "summary": "Group summary",
                            "summaries": {"0": "First on its own", "1": "Second on its own"},
                            "article_ids": [0, 1],
                        }
                    ]
                }
            )
        )

        sections = await summarize_articles(
            [*sample_summarize_articles, second],
            llm,
            usage=usage,
            snippet_length=1000,
            output_language="zh-Hant",
            style_prompt="",
        )

        assert sections[0].summary == "Group summary"
        assert [item.summary for item in sections[0].items] == [
            "First on its own",
            "Second on its own",
        ]
        assert [item.passed_through for item in sections[0].items] == [False, False]
        assert usage.fell_back_to_excerpts is False
        assert '"summaries"' in llm.calls[0]["system"]

    @pytest.mark.parametrize(
        "summaries",
        [{}, {"0": ""}, {"0": 7}, "not an object"],
        ids=["absent", "blank", "not-text", "malformed"],
    )
    async def test_a_missing_article_summary_falls_back_to_its_excerpt(
        self, sample_summarize_articles, summaries
    ):
        article = sample_summarize_articles[0]
        usage = UsageStats(model="m")
        llm = FakeLLM(
            json.dumps(
                {
                    "sections": [
                        {
                            "heading": "H",
                            "summary": "Group summary",
                            "summaries": summaries,
                            "article_ids": [0],
                        }
                    ]
                }
            )
        )

        sections = await summarize_articles(
            [article],
            llm,
            usage=usage,
            snippet_length=1000,
            output_language="zh-Hant",
            style_prompt="",
        )

        assert sections[0].items[0].summary == excerpt(article.content)
        assert sections[0].items[0].passed_through is True
        assert sections[0].summary == "Group summary"
        assert usage.fell_back_to_excerpts is True

    async def test_a_group_without_a_summary_has_none_to_render_once(
        self, sample_summarize_articles
    ):
        llm = FakeLLM(json.dumps({"sections": [{"heading": "H", "article_ids": [0]}]}))

        sections = await summarize_articles(
            sample_summarize_articles,
            llm,
            snippet_length=1000,
            output_language="zh-Hant",
            style_prompt="",
        )

        assert sections[0].summary is None

    async def test_summarize_metadata_comes_from_local_article(self):
        """Title/source/url come from the store article, never the LLM echo —
        URL-string ids (newsletters) must not lose their links."""
        from datetime import datetime

        from cyris.domain.models import Article

        article = Article(
            id="https://blog.example.com/p/a-long-slug",
            title="Original Title",
            url="https://blog.example.com/p/a-long-slug",
            content="Content",
            published_at=datetime(2026, 8, 14, tzinfo=UTC),
            source_name="Example Newsletter",
            source_lane=Lane.READ,
            source_tags=["tech"],
        )
        llm = FakeLLM(
            json.dumps(
                {
                    "sections": [
                        {
                            "heading": "Heading",
                            "summary": "Summary",
                            "article_ids": [0],
                        }
                    ]
                }
            )
        )

        item = (
            await summarize_articles(
                [article],
                llm,
                article_scores={article.url: 88.0},
                snippet_length=1000,
                output_language="zh-Hant",
                style_prompt="",
            )
        )[0].items[0]

        assert item.title == "Original Title"
        assert item.sources == ["Example Newsletter"]
        assert item.urls == [article.url]
        assert item.score == 88.0

    async def test_summarize_skips_unknown_ids(self):
        """Out-of-range or non-numeric ids are dropped instead of producing linkless items."""
        llm = FakeLLM(
            json.dumps(
                {
                    "sections": [
                        {
                            "heading": "Heading",
                            "summary": "Summary",
                            "article_ids": [5, "not-an-id"],
                        }
                    ]
                }
            )
        )
        from datetime import datetime

        from cyris.domain.models import Article

        article = Article(
            id=1,
            title="Article",
            url="https://example.com/1",
            content="Content",
            published_at=datetime(2026, 8, 14, tzinfo=UTC),
            source_name="Source",
            source_lane=Lane.READ,
            source_tags=["tech"],
        )

        sections = await summarize_articles(
            [article], llm, snippet_length=1000, output_language="zh-Hant", style_prompt=""
        )

        assert sections[0].items == []

    async def test_summarize_empty_input(self):
        sections = await summarize_articles(
            [], FakeLLM(), snippet_length=1000, output_language="zh-Hant", style_prompt=""
        )
        assert sections == []

    async def test_summarize_articles_passes_temperature(self, sample_summarize_articles):
        """summarize_articles should pass temperature=1.0 to the LLM."""
        llm = FakeLLM(json.dumps({"sections": []}))

        await summarize_articles(
            sample_summarize_articles,
            llm,
            snippet_length=1000,
            output_language="zh-Hant",
            style_prompt="",
        )

        assert len(llm.calls) == 1
        assert llm.calls[0]["temperature"] == 1.0


class TestDigestItemsCarryRefUrls:
    def test_fan_item_keeps_ref_urls_without_changing_store_url(self):
        from datetime import datetime

        from cyris.domain.models import Article
        from cyris.service_layer.summarize import build_follow_sections

        article = Article(
            id=1,
            title="Newsletter",
            url="newsletter:abc",
            content="Content",
            published_at=datetime(2026, 4, 10, tzinfo=UTC),
            source_name="Newsletter",
            source_lane=Lane.FOLLOW,
            ref_urls=["https://r1.com/a"],
        )

        item = build_follow_sections([article])[0].items[0]

        assert item.ref_urls == ["https://r1.com/a"]
        assert item.urls == ["newsletter:abc"]

    async def test_summarized_item_keeps_ref_urls_without_changing_store_url(self):
        from datetime import datetime

        from cyris.domain.models import Article

        article = Article(
            id=1,
            title="Article",
            url="newsletter:abc",
            content="Content",
            published_at=datetime(2026, 4, 10, tzinfo=UTC),
            source_name="Newsletter",
            source_lane=Lane.READ,
            ref_urls=["https://r1.com/a"],
        )
        llm = FakeLLM(
            json.dumps(
                {
                    "sections": [
                        {
                            "heading": "Heading",
                            "summary": "Summary",
                            "article_ids": [0],
                        }
                    ]
                }
            )
        )

        item = (
            await summarize_articles(
                [article], llm, snippet_length=1000, output_language="zh-Hant", style_prompt=""
            )
        )[0].items[0]

        assert item.ref_urls == ["https://r1.com/a"]
        assert item.urls == ["newsletter:abc"]

    def test_degraded_item_keeps_ref_urls_without_changing_store_url(self):
        from datetime import datetime

        from cyris.domain.models import Article
        from cyris.service_layer.degrade import excerpt_sections_from_articles

        article = Article(
            id=1,
            title="Article",
            url="newsletter:abc",
            content="Content",
            published_at=datetime(2026, 4, 10, tzinfo=UTC),
            source_name="Newsletter",
            source_lane=Lane.READ,
            ref_urls=["https://r1.com/a"],
        )

        section = excerpt_sections_from_articles([article])[0]
        item = section.items[0]

        assert item.ref_urls == ["https://r1.com/a"]
        assert item.urls == ["newsletter:abc"]
        # Each excerpt is its own article's, so there is no group summary to render once.
        assert section.summary is None

    async def test_rss_items_default_to_empty_ref_urls(self):
        from datetime import datetime

        from cyris.domain.models import Article
        from cyris.service_layer.degrade import excerpt_sections_from_articles
        from cyris.service_layer.summarize import build_follow_sections

        fan_article = Article(
            id=1,
            title="Fan",
            url="https://example.com/fan",
            content="Content",
            published_at=datetime(2026, 4, 10, tzinfo=UTC),
            source_name="Source",
            source_lane=Lane.FOLLOW,
        )
        summarize_article = fan_article.model_copy(update={"source_lane": Lane.READ})
        llm = FakeLLM(
            json.dumps(
                {
                    "sections": [
                        {
                            "heading": "Heading",
                            "summary": "Summary",
                            "article_ids": [0],
                        }
                    ]
                }
            )
        )

        fan_item = build_follow_sections([fan_article])[0].items[0]
        summarized_item = (
            await summarize_articles(
                [summarize_article],
                llm,
                snippet_length=1000,
                output_language="zh-Hant",
                style_prompt="",
            )
        )[0].items[0]
        degraded_item = excerpt_sections_from_articles([summarize_article])[0].items[0]

        assert [item.ref_urls for item in (fan_item, summarized_item, degraded_item)] == [
            [],
            [],
            [],
        ]
