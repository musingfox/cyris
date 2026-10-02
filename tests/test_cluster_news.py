"""Tests for news clustering."""

import json
import re
from datetime import UTC, datetime

import pytest
from fakes import FakeLLM

from cyris.domain.models import Article, Tier, UsageStats
from cyris.service_layer.cluster_news import cluster_news, filter_news

pytestmark = pytest.mark.unit


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
                        "article_ids": [0, 1]
                    }
                ],
                "unclustered_ids": [2]
            }""",
            input_tokens=100,
            output_tokens=50,
        )

        usage = UsageStats(model="claude-sonnet-4-6")
        clusters, unclustered = await cluster_news(
            sample_news_articles,
            llm,
            usage=usage,
            snippet_length=500,
            output_language="zh-Hant",
            style_prompt="",
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
            '{"clusters": [{"heading": "Mixed", "summary": "s", "article_ids": [0, 1]}], '
            '"unclustered_ids": []}'
        )

        clusters, unclustered = await cluster_news(
            articles, llm, snippet_length=500, output_language="zh-Hant", style_prompt=""
        )

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
            '{"clusters": [{"heading": "Syn", "summary": "s", "article_ids": [0, 1]}], '
            '"unclustered_ids": []}'
        )

        clusters, _ = await cluster_news(
            articles, llm, snippet_length=500, output_language="zh-Hant", style_prompt=""
        )

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
            '{"clusters": [{"heading": "Dup", "summary": "s", "article_ids": [0, 1]}], '
            '"unclustered_ids": []}'
        )

        clusters, _ = await cluster_news(
            articles, llm, snippet_length=500, output_language="zh-Hant", style_prompt=""
        )

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
            '{"clusters": [{"heading": "Plain", "summary": "s", "article_ids": [0, 1]}], '
            '"unclustered_ids": []}'
        )

        clusters, unclustered = await cluster_news(
            articles, llm, snippet_length=500, output_language="zh-Hant", style_prompt=""
        )

        item = clusters[0].items[0]
        assert item.ref_urls == []
        assert item.urls == ["https://store.example/first", "https://store.example/second"]
        assert item.sources == ["Source A", "Source B"]
        assert unclustered == []

    async def test_cluster_news_no_clusters(self, sample_news_articles):
        llm = FakeLLM('{"clusters": [], "unclustered_ids": [0, 1, 2]}')

        clusters, unclustered = await cluster_news(
            sample_news_articles,
            llm,
            snippet_length=500,
            output_language="zh-Hant",
            style_prompt="",
        )

        assert len(clusters) == 0
        assert len(unclustered) == 3

    async def test_cluster_without_tags_keeps_empty_tags(self, sample_news_articles):
        llm = FakeLLM('{"clusters": [{"heading": "H", "summary": "S", "article_ids": [0, 1]}]}')

        clusters, unclustered = await cluster_news(
            sample_news_articles,
            llm,
            snippet_length=500,
            output_language="zh-Hant",
            style_prompt="",
        )

        assert clusters[0].heading == "H"
        assert clusters[0].tags == []
        assert [article.id for article in unclustered] == [103]

    async def test_null_tags_do_not_destroy_the_windows_clustering(self, sample_news_articles):
        """One malformed tags value must never degrade the whole window to unclustered."""
        llm = FakeLLM(
            '{"clusters": ['
            '{"heading": "A", "summary": "S", "article_ids": [0], "tags": null}, '
            '{"heading": "B", "summary": "S", "article_ids": [1], "tags": ["AI Policy"]}]}'
        )

        clusters, unclustered = await cluster_news(
            sample_news_articles,
            llm,
            snippet_length=500,
            output_language="zh-Hant",
            style_prompt="",
        )

        assert [c.heading for c in clusters] == ["A", "B"]
        assert clusters[0].tags == []
        assert clusters[1].tags == ["ai policy"]
        assert [article.id for article in unclustered] == [103]

    async def test_string_tags_value_becomes_a_single_tag(self, sample_news_articles):
        llm = FakeLLM(
            '{"clusters": ['
            '{"heading": "A", "summary": "S", "article_ids": [0], "tags": "AI"}, '
            '{"heading": "B", "summary": "S", "article_ids": [1], "tags": ["ML"]}]}'
        )

        clusters, _ = await cluster_news(
            sample_news_articles,
            llm,
            snippet_length=500,
            output_language="zh-Hant",
            style_prompt="",
        )

        assert [c.heading for c in clusters] == ["A", "B"]
        assert clusters[0].tags == ["ai"]  # one tag, not per-character shards
        assert clusters[1].tags == ["ml"]

    async def test_non_string_tag_elements_are_dropped_not_fatal(self, sample_news_articles):
        llm = FakeLLM(
            '{"clusters": ['
            '{"heading": "A", "summary": "S", "article_ids": [0], "tags": ["AI", 42]}, '
            '{"heading": "B", "summary": "S", "article_ids": [1], "tags": ["ML"]}]}'
        )

        clusters, _ = await cluster_news(
            sample_news_articles,
            llm,
            snippet_length=500,
            output_language="zh-Hant",
            style_prompt="",
        )

        assert [c.heading for c in clusters] == ["A", "B"]
        assert clusters[0].tags == ["ai"]
        assert clusters[1].tags == ["ml"]

    async def test_articles_the_model_forgot_to_mention_are_not_lost(self, sample_news_articles):
        """The response clusters one article and names none as unclustered.

        Read literally that means "throw the other two away". Measured on a real
        143-article window, llama-4-scout answered exactly this shape and named
        4 — trusting it dropped 139 articles out of the run with no error.
        """
        llm = FakeLLM(
            '{"clusters": [{"heading": "H", "summary": "S", "article_ids": [0]}],'
            ' "unclustered_ids": []}'
        )

        clusters, unclustered = await cluster_news(
            sample_news_articles,
            llm,
            snippet_length=500,
            output_language="zh-Hant",
            style_prompt="",
        )

        assert len(clusters) == 1
        assert [a.id for a in unclustered] == [102, 103]

    async def test_an_empty_response_leaves_every_article_unclustered(self, sample_news_articles):
        """`{"clusters": [], "unclustered_ids": []}` is a valid reply, not an error.

        gemini-3.6-flash produced it on a live window; the whole window vanished.
        """
        llm = FakeLLM('{"clusters": [], "unclustered_ids": []}')

        clusters, unclustered = await cluster_news(
            sample_news_articles,
            llm,
            snippet_length=500,
            output_language="zh-Hant",
            style_prompt="",
        )

        assert clusters == []
        assert unclustered == sample_news_articles

    async def test_an_id_the_model_invented_does_not_disturb_the_rest(self, sample_news_articles):
        llm = FakeLLM(
            '{"clusters": [{"heading": "H", "summary": "S", "article_ids": [0, 999]}],'
            ' "unclustered_ids": []}'
        )

        clusters, unclustered = await cluster_news(
            sample_news_articles,
            llm,
            snippet_length=500,
            output_language="zh-Hant",
            style_prompt="",
        )

        assert len(clusters[0].items[0].sources) == 1  # 999 contributed nothing
        assert [a.id for a in unclustered] == [102, 103]

    async def test_cluster_news_empty_input(self):
        clusters, unclustered = await cluster_news(
            [], FakeLLM(), snippet_length=500, output_language="zh-Hant", style_prompt=""
        )

        assert clusters == []
        assert unclustered == []

    async def test_cluster_news_api_failure(self, sample_news_articles):
        llm = FakeLLM(error=Exception("API Error"))

        clusters, unclustered = await cluster_news(
            sample_news_articles,
            llm,
            snippet_length=500,
            output_language="zh-Hant",
            style_prompt="",
        )

        assert len(clusters) == 0
        assert len(unclustered) == 3
        assert unclustered == sample_news_articles


class _NumericTailLLM(FakeLLM):
    """Answers like @cf/openai/gpt-oss-120b did on 2026-09-17: it clusters every
    article it was shown, but writes each id as the integer tail of the one in the
    prompt ('CNA/2026-09-17/202609170255' comes back as 255)."""

    async def complete(self, prompt, **kwargs):
        ids = re.findall(r"^\[(.+?)\]", prompt, flags=re.MULTILINE)
        tails = [int(re.search(r"(\d{1,4})$", i).group(1)) for i in ids]
        self._responses = [
            json.dumps(
                {
                    "clusters": [
                        {"heading": "h", "summary": "s", "article_ids": tails, "tags": ["t"]}
                    ],
                    "unclustered_ids": [],
                }
            )
        ]
        return await super().complete(prompt, **kwargs)


def _cna_articles() -> list[Article]:
    return [
        Article(
            id=f"CNA/2026-09-17/2026091702{n}",
            title=f"Article {n}",
            url=f"https://www.cna.com.tw/news/{n}",
            content="Content",
            published_at=datetime(2026, 9, 17, 10, 0, tzinfo=UTC),
            source_name="中央社即時新聞 財經新聞",
            source_tier=Tier.FILTER,
            source_tags=["news"],
        )
        for n in ("55", "47")
    ]


async def test_cluster_survives_a_model_that_rewrites_string_ids():
    articles = _cna_articles()

    clusters, unclustered = await cluster_news(
        articles, _NumericTailLLM(), snippet_length=500, output_language="zh-Hant", style_prompt=""
    )

    assert len(clusters) == 1
    assert clusters[0].items[0].urls == [a.url for a in articles]
    assert unclustered == []


async def test_a_position_echoed_as_a_string_still_resolves():
    articles = _cna_articles()
    llm = FakeLLM('{"clusters": [{"heading": "H", "summary": "S", "article_ids": ["0", "1"]}]}')

    clusters, unclustered = await cluster_news(
        articles, llm, snippet_length=500, output_language="zh-Hant", style_prompt=""
    )

    assert clusters[0].items[0].urls == [a.url for a in articles]
    assert unclustered == []


async def test_the_snippet_follows_the_given_length():
    article = _cna_articles()[0].model_copy(update={"content": "A" * 600})
    llm = FakeLLM('{"clusters": []}')

    await cluster_news(
        [article], llm, snippet_length=120, output_language="zh-Hant", style_prompt=""
    )

    assert "A" * 120 in llm.calls[0]["prompt"]
    assert "A" * 121 not in llm.calls[0]["prompt"]
